import subprocess
import boto3
import os
import json
import logging
from datetime import datetime
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_influx_token(secret_arn):
    """Fetch InfluxDB token from Secrets Manager."""
    try:
        secrets_client = boto3.client("secretsmanager")
        response = secrets_client.get_secret_value(SecretId=secret_arn)
        return response["SecretString"]
    except ClientError as e:
        logger.error(f"Failed to retrieve secret {secret_arn}: {str(e)}")
        raise

def lambda_handler(event, context):
    try:
        # Initialize variables from environment
        influx_bin = "/opt/bin/influx"
        restore_base_path = "/tmp/restore"
        influx_url = os.environ.get("INFLUXDB_URL")
        influx_token_secret_arn = os.environ.get("INFLUXDB_TOKEN")
        influx_org = os.environ.get("INFLUXDB_ORG")
        influx_new_org = os.environ.get("INFLUXDB_NEW_ORG")
        s3_bucket = os.environ.get("S3_BUCKET")

        # Validate environment variables
        if not all([influx_url, influx_token_secret_arn, influx_org, s3_bucket]):
            raise ValueError("Missing required environment variables: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, or S3_BUCKET")

        # Get backup timestamp from event (format: YYYYMMDDTHHMMSSZ)
        backup_timestamp = event.get("backup_timestamp")
        if not backup_timestamp:
            raise ValueError("Missing 'backup_timestamp' in event (format: YYYYMMDDTHHMMSSZ)")
        try:
            datetime.strptime(backup_timestamp, "%Y%m%dT%H%M%SZ")
        except ValueError:
            raise ValueError("Invalid 'backup_timestamp' format, expected YYYYMMDDTHHMMSSZ")

        # Fetch InfluxDB token
        logger.info("Retrieving InfluxDB token from Secrets Manager")
        influx_token = get_influx_token(influx_token_secret_arn)

        # S3 configuration
        s3_client = boto3.client("s3")

        # Default bucket configuration if not provided in event or environment asset_bucket, cloud_bucket
        buckets = [
            {"name": "asset_bucket", "s3_path": f"influx-backups/monthly/{backup_timestamp}/asset_bucket/", "dest_bucket": "restored_asset_bucket"},
            {"name": "cloud_bucket", "s3_path": f"influx-backups/monthly/{backup_timestamp}/cloud_bucket/", "dest_bucket": "restored_cloud_bucket"}
        ]

        # Create temporary directory
        logger.info(f"Creating restore directory at {restore_base_path}")
        os.makedirs(restore_base_path, exist_ok=True)

        # Restore each bucket
        restored_buckets = []
        for bucket in buckets:
            bucket_name = bucket["name"]
            dest_bucket = bucket["dest_bucket"]
            s3_prefix = bucket["s3_path"]
            restore_path = f"{restore_base_path}/{bucket_name}"
            os.makedirs(restore_path, exist_ok=True)

            # Download backup files from S3
            logger.info(f"Downloading backup files for {bucket_name} from s3://{s3_bucket}/{s3_prefix}")
            try:
                response = s3_client.list_objects_v2(Bucket=s3_bucket, Prefix=s3_prefix)
                if "Contents" not in response:
                    logger.warning(f"No backup files found for {bucket_name} at {s3_prefix}")
                    continue
                for obj in response["Contents"]:
                    file_name = os.path.basename(obj["Key"])
                    file_path = os.path.join(restore_path, file_name)
                    s3_client.download_file(Bucket=s3_bucket, Key=obj["Key"], Filename=file_path)
                    logger.info(f"Downloaded {file_name} to {file_path}")
            except ClientError as e:
                logger.error(f"Failed to list or download files for {bucket_name}: {str(e)}")
                raise

            # Run restore command with --new-bucket
            logger.info(f"Restoring {bucket_name} to new bucket {dest_bucket} from {restore_path}")
            cmd = [
                influx_bin, "restore", restore_path,
                "--host", influx_url,
                "--org", influx_org,
                "--token", influx_token,
                "--bucket", bucket_name,
                "--new-org", influx_new_org,
                "--new-bucket", dest_bucket
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                logger.error(f"Restore failed for {bucket_name} to {dest_bucket}: {result.stderr}")
                raise Exception(f"Restore failed for {bucket_name}: {result.stderr}")

            # Clean up restore directory
            for file_name in os.listdir(restore_path):
                file_path = os.path.join(restore_path, file_name)
                try:
                    os.remove(file_path)
                    logger.debug(f"Deleted local file {file_path}")
                except OSError as e:
                    logger.warning(f"Failed to delete {file_path}: {str(e)}")
            try:
                os.rmdir(restore_path)
                logger.debug(f"Deleted restore directory {restore_path}")
            except OSError as e:
                logger.warning(f"Failed to delete directory {restore_path}: {str(e)}")

            logger.info(f"Restored {bucket_name} to {dest_bucket}")
            restored_buckets.append(dest_bucket)

        # Clean up base restore directory
        try:
            os.rmdir(restore_base_path)
            logger.info(f"Removed temporary directory {restore_base_path}")
        except OSError as e:
            logger.warning(f"Failed to remove {restore_base_path}: {str(e)}")

        logger.info(f"Restoration completed for {backup_timestamp} to buckets {restored_buckets}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Restoration completed for {backup_timestamp} to buckets {restored_buckets}",
                "buckets": restored_buckets
            })
        }

    except subprocess.TimeoutExpired as e:
        logger.error(f"Operation timed out: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Operation timed out: {str(e)}"})
        }
    except Exception as e:
        logger.error(f"Operation failed: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }