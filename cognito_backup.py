import json
import boto3
import logging
from datetime import datetime
from typing import Dict, Any, List
import os

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class CognitoBackupRestore:
    def __init__(self):
        self.cognito_client = boto3.client('cognito-idp')
        self.s3_client = boto3.client('s3')
        self.bucket_name = os.environ.get('BACKUP_BUCKET_NAME')
        
    def backup_user_pool(self, user_pool_id: str) -> Dict[str, Any]:
        """Backup Cognito User Pool users and groups only (no clients)"""
        try:
            # Get user pool details
            user_pool = self.cognito_client.describe_user_pool(UserPoolId=user_pool_id)
            
            # Get users (paginated) with embedded group memberships
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
                        user_groups = [group['GroupName'] for group in user_groups_response['Groups']]
                        if user_groups:
                            logger.info(f"User {username} belongs to groups: {user_groups}")
                    except Exception as e:
                        logger.warning(f"Could not retrieve groups for user {username}: {e}")
                    
                    # Add groups to user object
                    user['Groups'] = user_groups
                    users.append(user)
            
            # Get groups
            groups = []
            try:
                groups_response = self.cognito_client.list_groups(UserPoolId=user_pool_id)
                groups = groups_response['Groups']
            except Exception as e:
                logger.warning(f"Could not retrieve groups: {e}")
            
            # Create backup object (no clients)
            backup_data = {
                'timestamp': datetime.utcnow().isoformat(),
                'user_pool': user_pool['UserPool'],
                'users': users,
                'groups': groups
            }
            
            # Save to S3
            backup_key = f"cognito-backups/{user_pool_id}/{datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')}.json"
            
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=backup_key,
                Body=json.dumps(backup_data, default=str),
                ContentType='application/json'
            )
            
            logger.info(f"Backup completed for user pool {user_pool_id}")
            return {
                'status': 'success',
                'backup_location': f"s3://{self.bucket_name}/{backup_key}",
                'users_backed_up': len(users),
                'groups_backed_up': len(groups)
            }
            
        except Exception as e:
            logger.error(f"Backup failed for user pool {user_pool_id}: {str(e)}")
            raise
    
    def restore_user_pool(self, backup_key: str, target_user_pool_id: str = None) -> Dict[str, Any]:
        """Restore Cognito User Pool users and groups only (no clients)"""
        try:
            # Get backup data from S3
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=backup_key)
            backup_data = json.loads(response['Body'].read())
            
            if target_user_pool_id:
                # Restore to existing user pool
                user_pool_id = target_user_pool_id
                logger.info(f"Restoring to existing user pool: {user_pool_id}")
            else:
                # Create new user pool (simplified - you may need to adjust based on your requirements)
                user_pool_config = backup_data['user_pool'].copy()
                # Remove read-only fields
                read_only_fields = ['Id', 'Name', 'Status', 'CreationDate', 'LastModifiedDate', 'Arn']
                for field in read_only_fields:
                    user_pool_config.pop(field, None)
                
                create_response = self.cognito_client.create_user_pool(**user_pool_config)
                user_pool_id = create_response['UserPool']['Id']
                logger.info(f"Created new user pool: {user_pool_id}")
            
            # Restore groups first (users need groups to exist before membership assignment)
            restored_groups = 0
            for group in backup_data['groups']:
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
                    logger.info(f"Restored group: {group['GroupName']}")
                    
                except Exception as e:
                    if 'GroupExistsException' in str(e):
                        logger.info(f"Group {group['GroupName']} already exists, skipping")
                        restored_groups += 1
                    else:
                        logger.warning(f"Failed to restore group {group['GroupName']}: {e}")
            
            # Restore users (with embedded group information)
            restored_users = 0
            restored_memberships = 0
            failed_users = []
            
            for user in backup_data['users']:
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
                    logger.info(f"Restored user: {username}")
                    
                    # Restore user's group memberships
                    for group_name in user_groups:
                        try:
                            self.cognito_client.admin_add_user_to_group(
                                UserPoolId=user_pool_id,
                                Username=username,
                                GroupName=group_name
                            )
                            restored_memberships += 1
                            logger.info(f"Added user {username} to group {group_name}")
                            
                        except Exception as e:
                            logger.warning(f"Failed to add user {username} to group {group_name}: {e}")
                    
                except Exception as e:
                    if 'UsernameExistsException' in str(e):
                        logger.info(f"User {username} already exists, skipping user creation")
                        restored_users += 1
                        
                        # Still try to restore group memberships for existing users
                        for group_name in user_groups:
                            try:
                                self.cognito_client.admin_add_user_to_group(
                                    UserPoolId=user_pool_id,
                                    Username=username,
                                    GroupName=group_name
                                )
                                restored_memberships += 1
                                logger.info(f"Added existing user {username} to group {group_name}")
                            except Exception as group_e:
                                logger.warning(f"Failed to add existing user {username} to group {group_name}: {group_e}")
                    else:
                        logger.warning(f"Failed to restore user {username}: {e}")
                        failed_users.append(username)
            
            logger.info(f"Restore completed for user pool {user_pool_id}")
            return {
                'status': 'success',
                'user_pool_id': user_pool_id,
                'users_restored': restored_users,
                'groups_restored': restored_groups,
                'user_group_memberships_restored': restored_memberships,
                'failed_users': failed_users,
                'backup_timestamp': backup_data['timestamp']
            }
            
        except Exception as e:
            logger.error(f"Restore failed: {str(e)}")
            raise

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda handler"""
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
            
        elif operation == 'restore':
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
            
        else:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid operation. Use "backup" or "restore"'})
            }
            
    except Exception as e:
        logger.error(f"Lambda execution failed: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
