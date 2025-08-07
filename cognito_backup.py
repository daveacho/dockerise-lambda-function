"""
AWS Lambda function for Cognito User Pool backup and restore operations.

This module provides functionality to backup and restore Amazon Cognito User Pools,
including users and groups (but not clients). Users are backed up with their
group memberships embedded for easier restoration.
"""

import json
import logging
import os
from datetime import datetime, UTC
from typing import Dict, Any

import boto3

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


class CognitoBackupRestore:
    """
    Handles backup and restore operations for AWS Cognito User Pools.
    
    This class provides methods to backup user pool data (users and groups)
    to S3 and restore from S3 backups. Client configurations are not included.
    """

    def __init__(self):
        """Initialize the CognitoBackupRestore with AWS clients."""
        self.cognito_client = boto3.client('cognito-idp')
        self.s3_client = boto3.client('s3')
        self.bucket_name = os.environ.get('BACKUP_BUCKET_NAME')

    def backup_user_pool(self, user_pool_id: str) -> Dict[str, Any]:
        """
        Backup Cognito User Pool users and groups only (no clients).
        
        Args:
            user_pool_id: The ID of the user pool to backup
            
        Returns:
            Dict containing backup status and statistics
            
        Raises:
            Exception: If backup operation fails
        """
        try:
            # Get user pool details
            user_pool = self.cognito_client.describe_user_pool(
                UserPoolId=user_pool_id
            )

            # Get users (paginated) with embedded group memberships
            users = self._get_users_with_groups(user_pool_id)

            # Get groups
            groups = self._get_groups(user_pool_id)

            # Create backup object (no clients)
            backup_data = {
                'timestamp': datetime.now(UTC).isoformat(),
                'user_pool': user_pool['UserPool'],
                'users': users,
                'groups': groups
            }

            # Save to S3
            backup_key = (
                f"cognito-backups/{user_pool_id}/"
                f"{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.json"
            )

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=backup_key,
                Body=json.dumps(backup_data, default=str, indent=2),
                ContentType='application/json'
            )

            logger.info("Backup completed for user pool %s", user_pool_id)
            return {
                'status': 'success',
                'backup_location': f"s3://{self.bucket_name}/{backup_key}",
                'users_backed_up': len(users),
                'groups_backed_up': len(groups)
            }

        except Exception as exc:
            logger.error("Backup failed for user pool %s: %s", user_pool_id, str(exc))
            raise

    def _get_users_with_groups(self, user_pool_id: str) -> list:
        """
        Get all users from the user pool with their group memberships embedded.
        
        Args:
            user_pool_id: The ID of the user pool
            
        Returns:
            List of user objects with embedded group memberships
        """
        users = []
        paginator = self.cognito_client.get_paginator('list_users')
        
        for page in paginator.paginate(UserPoolId=user_pool_id):
            for user in page['Users']:
                # Enhance user object with group memberships
                username = user['Username']
                user_groups = []
                try:
                    user_groups_response = self.cognito_client.admin_list_groups_for_user(
                        UserPoolId=user_pool_id,
                        Username=username
                    )
                    user_groups = [
                        group['GroupName'] for group in user_groups_response['Groups']
                    ]
                    if user_groups:
                        logger.info("User %s belongs to groups: %s", username, user_groups)
                except Exception as exc:
                    logger.warning("Could not retrieve groups for user %s: %s", username, exc)

                # Add groups to user object
                user['Groups'] = user_groups
                users.append(user)
        
        return users

    def _get_groups(self, user_pool_id: str) -> list:
        """
        Get all groups from the user pool.
        
        Args:
            user_pool_id: The ID of the user pool
            
        Returns:
            List of group objects
        """
        groups = []
        try:
            groups_response = self.cognito_client.list_groups(UserPoolId=user_pool_id)
            groups = groups_response['Groups']
        except Exception as exc:
            logger.warning("Could not retrieve groups: %s", exc)
        
        return groups

    def restore_user_pool(self, backup_key: str, target_user_pool_id: str = None) -> Dict[str, Any]:
        """
        Restore Cognito User Pool users and groups only (no clients).
        
        Args:
            backup_key: S3 key of the backup file
            target_user_pool_id: Optional target user pool ID for restoration
            
        Returns:
            Dict containing restore status and statistics
            
        Raises:
            Exception: If restore operation fails
        """
        try:
            # Get backup data from S3
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=backup_key)
            backup_data = json.loads(response['Body'].read())

            user_pool_id = self._get_or_create_user_pool(backup_data, target_user_pool_id)

            # Restore groups first (users need groups to exist before membership assignment)
            restored_groups = self._restore_groups(backup_data['groups'], user_pool_id)

            # Restore users (with embedded group information)
            restore_stats = self._restore_users(backup_data['users'], user_pool_id)

            logger.info("Restore completed for user pool %s", user_pool_id)
            return {
                'status': 'success',
                'user_pool_id': user_pool_id,
                'users_restored': restore_stats['users_restored'],
                'groups_restored': restored_groups,
                'user_group_memberships_restored': restore_stats['memberships_restored'],
                'failed_users': restore_stats['failed_users'],
                'backup_timestamp': backup_data['timestamp']
            }

        except Exception as exc:
            logger.error("Restore failed: %s", str(exc))
            raise

    def _get_or_create_user_pool(self, backup_data: dict, target_user_pool_id: str = None) -> str:
        """
        Get existing user pool ID or create a new one.
        
        Args:
            backup_data: The backup data containing user pool configuration
            target_user_pool_id: Optional target user pool ID
            
        Returns:
            User pool ID to use for restoration
        """
        if target_user_pool_id:
            # Restore to existing user pool
            logger.info("Restoring to existing user pool: %s", target_user_pool_id)
            return target_user_pool_id

        # Create new user pool
        user_pool_config = backup_data['user_pool'].copy()
        # Remove read-only fields
        read_only_fields = ['Id', 'Name', 'Status', 'CreationDate', 'LastModifiedDate', 'Arn']
        for field in read_only_fields:
            user_pool_config.pop(field, None)

        create_response = self.cognito_client.create_user_pool(**user_pool_config)
        user_pool_id = create_response['UserPool']['Id']
        logger.info("Created new user pool: %s", user_pool_id)
        return user_pool_id

    def _restore_groups(self, groups: list, user_pool_id: str) -> int:
        """
        Restore groups to the user pool.
        
        Args:
            groups: List of group objects to restore
            user_pool_id: Target user pool ID
            
        Returns:
            Number of groups restored
        """
        restored_groups = 0
        for group in groups:
            try:
                group_config = {
                    'GroupName': group['GroupName'],
                    'UserPoolId': user_pool_id
                }
                if 'Description' in group:
                    group_config['Description'] = group['Description']
                if 'Precedence' in group:
                    group_config['Precedence'] = group['Precedence']

                self.cognito_client.create_group(**group_config)
                restored_groups += 1
                logger.info("Restored group: %s", group['GroupName'])

            except Exception as exc:
                if 'GroupExistsException' in str(exc):
                    logger.info("Group %s already exists, skipping", group['GroupName'])
                    restored_groups += 1
                else:
                    logger.warning("Failed to restore group %s: %s", group['GroupName'], exc)

        return restored_groups

    def _restore_users(self, users: list, user_pool_id: str) -> Dict[str, Any]:
        """
        Restore users to the user pool with their group memberships.
        
        Args:
            users: List of user objects to restore
            user_pool_id: Target user pool ID
            
        Returns:
            Dict containing restoration statistics
        """
        restored_users = 0
        restored_memberships = 0
        failed_users = []

        for user in users:
            try:
                username = user['Username']
                user_groups = user.get('Groups', [])

                # Create user
                user_attributes = [
                    {'Name': attr['Name'], 'Value': attr['Value']}
                    for attr in user.get('Attributes', [])
                    if attr['Name'] not in ['sub']  # Skip system attributes
                ]

                self.cognito_client.admin_create_user(
                    UserPoolId=user_pool_id,
                    Username=username,
                    UserAttributes=user_attributes,
                    MessageAction='SUPPRESS',
                    TemporaryPassword='TempPass123!'
                )

                # Set permanent password if user was confirmed
                if user.get('UserStatus') == 'CONFIRMED':
                    self.cognito_client.admin_set_user_password(
                        UserPoolId=user_pool_id,
                        Username=username,
                        Password='TempPass123!',
                        Permanent=True
                    )

                restored_users += 1
                logger.info("Restored user: %s", username)

                # Restore user's group memberships
                restored_memberships += self._restore_user_group_memberships(
                    user_pool_id, username, user_groups
                )

            except Exception as exc:
                if 'UsernameExistsException' in str(exc):
                    logger.info("User %s already exists, skipping user creation", username)
                    restored_users += 1

                    # Still try to restore group memberships for existing users
                    restored_memberships += self._restore_user_group_memberships(
                        user_pool_id, username, user_groups
                    )
                else:
                    logger.warning("Failed to restore user %s: %s", username, exc)
                    failed_users.append(username)

        return {
            'users_restored': restored_users,
            'memberships_restored': restored_memberships,
            'failed_users': failed_users
        }

    def _restore_user_group_memberships(self, user_pool_id: str, username: str, 
                                      user_groups: list) -> int:
        """
        Restore group memberships for a specific user.
        
        Args:
            user_pool_id: Target user pool ID
            username: Username to add to groups
            user_groups: List of group names the user should belong to
            
        Returns:
            Number of group memberships restored
        """
        memberships_restored = 0
        for group_name in user_groups:
            try:
                self.cognito_client.admin_add_user_to_group(
                    UserPoolId=user_pool_id,
                    Username=username,
                    GroupName=group_name
                )
                memberships_restored += 1
                logger.info("Added user %s to group %s", username, group_name)

            except Exception as exc:
                logger.warning("Failed to add user %s to group %s: %s", 
                             username, group_name, exc)

        return memberships_restored


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Main Lambda handler for Cognito backup and restore operations.
    
    Args:
        event: Lambda event containing operation details
        context: Lambda context (unused)
        
    Returns:
        Dict containing HTTP response with status and body
    """
    try:
        backup_restore = CognitoBackupRestore()

        operation = event.get('operation')

        if operation == 'backup':
            user_pool_id = event.get('user_pool_id')
            if not user_pool_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'user_pool_id is required for backup operation'})
                }

            result = backup_restore.backup_user_pool(user_pool_id)
            return {
                'statusCode': 200,
                'body': json.dumps(result)
            }

        if operation == 'restore':
            backup_key = event.get('backup_key')
            target_user_pool_id = event.get('target_user_pool_id')

            if not backup_key:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'backup_key is required for restore operation'})
                }

            result = backup_restore.restore_user_pool(backup_key, target_user_pool_id)
            return {
                'statusCode': 200,
                'body': json.dumps(result)
            }

        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Invalid operation. Use "backup" or "restore"'})
        }

    except Exception as exc:
        logger.error("Lambda execution failed: %s", str(exc))
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(exc)})
        }



# import json
# import boto3
# import logging
# from datetime import datetime, UTC
# from typing import Dict, Any, List
# import os

# # Configure logging
# logger = logging.getLogger()
# logger.setLevel(logging.INFO)

# class CognitoBackupRestore:
#     def __init__(self):
#         self.cognito_client = boto3.client('cognito-idp')
#         self.s3_client = boto3.client('s3')
#         self.bucket_name = os.environ.get('BACKUP_BUCKET_NAME')
        
#     def backup_user_pool(self, user_pool_id: str) -> Dict[str, Any]:
#         """Backup Cognito User Pool users and groups only (no clients)"""
#         try:
#             # Get user pool details
#             user_pool = self.cognito_client.describe_user_pool(UserPoolId=user_pool_id)
            
#             # Get users (paginated) with embedded group memberships
#             users = []
#             paginator = self.cognito_client.get_paginator('list_users')
#             for page in paginator.paginate(UserPoolId=user_pool_id):
#                 for user in page['Users']:
#                     # Enhance user object with group memberships
#                     username = user['Username']
#                     user_groups = []
#                     try:
#                         user_groups_response = self.cognito_client.admin_list_groups_for_user(
#                             UserPoolId=user_pool_id,
#                             Username=username
#                         )
#                         user_groups = [group['GroupName'] for group in user_groups_response['Groups']]
#                         if user_groups:
#                             logger.info(f"User {username} belongs to groups: {user_groups}")
#                     except Exception as e:
#                         logger.warning(f"Could not retrieve groups for user {username}: {e}")
                    
#                     # Add groups to user object
#                     user['Groups'] = user_groups
#                     users.append(user)
            
#             # Get groups
#             groups = []
#             try:
#                 groups_response = self.cognito_client.list_groups(UserPoolId=user_pool_id)
#                 groups = groups_response['Groups']
#             except Exception as e:
#                 logger.warning(f"Could not retrieve groups: {e}")
            
#             # Create backup object (no clients)
#             backup_data = {
#                 'timestamp': datetime.now(UTC).isoformat(),
#                 'user_pool': user_pool['UserPool'],
#                 'users': users,
#                 'groups': groups
#             }
            
#             # Save to S3
#             backup_key = f"cognito-backups/{user_pool_id}/{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.json"
            
#             self.s3_client.put_object(
#                 Bucket=self.bucket_name,
#                 Key=backup_key,
#                 Body=json.dumps(backup_data, default=str),
#                 ContentType='application/json'
#             )
            
#             logger.info(f"Backup completed for user pool {user_pool_id}")
#             return {
#                 'status': 'success',
#                 'backup_location': f"s3://{self.bucket_name}/{backup_key}",
#                 'users_backed_up': len(users),
#                 'groups_backed_up': len(groups)
#             }
            
#         except Exception as e:
#             logger.error(f"Backup failed for user pool {user_pool_id}: {str(e)}")
#             raise
    
#     def restore_user_pool(self, backup_key: str, target_user_pool_id: str = None) -> Dict[str, Any]:
#         """Restore Cognito User Pool users and groups only (no clients)"""
#         try:
#             # Get backup data from S3
#             response = self.s3_client.get_object(Bucket=self.bucket_name, Key=backup_key)
#             backup_data = json.loads(response['Body'].read())
            
#             if target_user_pool_id:
#                 # Restore to existing user pool
#                 user_pool_id = target_user_pool_id
#                 logger.info(f"Restoring to existing user pool: {user_pool_id}")
#             else:
#                 # Create new user pool (simplified - you may need to adjust based on your requirements)
#                 user_pool_config = backup_data['user_pool'].copy()
#                 # Remove read-only fields
#                 read_only_fields = ['Id', 'Name', 'Status', 'CreationDate', 'LastModifiedDate', 'Arn']
#                 for field in read_only_fields:
#                     user_pool_config.pop(field, None)
                
#                 create_response = self.cognito_client.create_user_pool(**user_pool_config)
#                 user_pool_id = create_response['UserPool']['Id']
#                 logger.info(f"Created new user pool: {user_pool_id}")
            
#             # Restore groups first (users need groups to exist before membership assignment)
#             restored_groups = 0
#             for group in backup_data['groups']:
#                 try:
#                     group_config = {
#                         'GroupName': group['GroupName'],
#                         'UserPoolId': user_pool_id
#                     }
#                     if 'Description' in group:
#                         group_config['Description'] = group['Description']
#                     if 'Precedence' in group:
#                         group_config['Precedence'] = group['Precedence']
                    
#                     self.cognito_client.create_group(**group_config)
#                     restored_groups += 1
#                     logger.info(f"Restored group: {group['GroupName']}")
                    
#                 except Exception as e:
#                     if 'GroupExistsException' in str(e):
#                         logger.info(f"Group {group['GroupName']} already exists, skipping")
#                         restored_groups += 1
#                     else:
#                         logger.warning(f"Failed to restore group {group['GroupName']}: {e}")
            
#             # Restore users (with embedded group information)
#             restored_users = 0
#             restored_memberships = 0
#             failed_users = []
            
#             for user in backup_data['users']:
#                 try:
#                     username = user['Username']
#                     user_groups = user.get('Groups', [])
                    
#                     # Create user
#                     user_attributes = [
#                         {'Name': attr['Name'], 'Value': attr['Value']}
#                         for attr in user.get('Attributes', [])
#                         if attr['Name'] not in ['sub']  # Skip system attributes
#                     ]
                    
#                     self.cognito_client.admin_create_user(
#                         UserPoolId=user_pool_id,
#                         Username=username,
#                         UserAttributes=user_attributes,
#                         MessageAction='SUPPRESS',
#                         TemporaryPassword='TempPass123!'
#                     )
                    
#                     # Set permanent password if user was confirmed
#                     if user.get('UserStatus') == 'CONFIRMED':
#                         self.cognito_client.admin_set_user_password(
#                             UserPoolId=user_pool_id,
#                             Username=username,
#                             Password='TempPass123!',
#                             Permanent=True
#                         )
                    
#                     restored_users += 1
#                     logger.info(f"Restored user: {username}")
                    
#                     # Restore user's group memberships
#                     for group_name in user_groups:
#                         try:
#                             self.cognito_client.admin_add_user_to_group(
#                                 UserPoolId=user_pool_id,
#                                 Username=username,
#                                 GroupName=group_name
#                             )
#                             restored_memberships += 1
#                             logger.info(f"Added user {username} to group {group_name}")
                            
#                         except Exception as e:
#                             logger.warning(f"Failed to add user {username} to group {group_name}: {e}")
                    
#                 except Exception as e:
#                     if 'UsernameExistsException' in str(e):
#                         logger.info(f"User {username} already exists, skipping user creation")
#                         restored_users += 1
                        
#                         # Still try to restore group memberships for existing users
#                         for group_name in user_groups:
#                             try:
#                                 self.cognito_client.admin_add_user_to_group(
#                                     UserPoolId=user_pool_id,
#                                     Username=username,
#                                     GroupName=group_name
#                                 )
#                                 restored_memberships += 1
#                                 logger.info(f"Added existing user {username} to group {group_name}")
#                             except Exception as group_e:
#                                 logger.warning(f"Failed to add existing user {username} to group {group_name}: {group_e}")
#                     else:
#                         logger.warning(f"Failed to restore user {username}: {e}")
#                         failed_users.append(username)
            
#             logger.info(f"Restore completed for user pool {user_pool_id}")
#             return {
#                 'status': 'success',
#                 'user_pool_id': user_pool_id,
#                 'users_restored': restored_users,
#                 'groups_restored': restored_groups,
#                 'user_group_memberships_restored': restored_memberships,
#                 'failed_users': failed_users,
#                 'backup_timestamp': backup_data['timestamp']
#             }
            
#         except Exception as e:
#             logger.error(f"Restore failed: {str(e)}")
#             raise

# def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
#     """Main Lambda handler"""
#     try:
#         backup_restore = CognitoBackupRestore()
        
#         operation = event.get('operation')
        
#         if operation == 'backup':
#             user_pool_id = event.get('user_pool_id')
#             if not user_pool_id:
#                 return {
#                     'statusCode': 400,
#                     'body': json.dumps({'error': 'user_pool_id is required for backup operation'})
#                 }
            
#             result = backup_restore.backup_user_pool(user_pool_id)
#             return {
#                 'statusCode': 200,
#                 'body': json.dumps(result)
#             }
            
#         elif operation == 'restore':
#             backup_key = event.get('backup_key')
#             target_user_pool_id = event.get('target_user_pool_id')
            
#             if not backup_key:
#                 return {
#                     'statusCode': 400,
#                     'body': json.dumps({'error': 'backup_key is required for restore operation'})
#                 }
            
#             result = backup_restore.restore_user_pool(backup_key, target_user_pool_id)
#             return {
#                 'statusCode': 200,
#                 'body': json.dumps(result)
#             }
            
#         else:
#             return {
#                 'statusCode': 400,
#                 'body': json.dumps({'error': 'Invalid operation. Use "backup" or "restore"'})
#             }
            
#     except Exception as e:
#         logger.error(f"Lambda execution failed: {str(e)}")
#         return {
#             'statusCode': 500,
#             'body': json.dumps({'error': str(e)})
#         }
