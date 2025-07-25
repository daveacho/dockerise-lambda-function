import subprocess
import boto3
import os
import json
import logging
import io
from datetime import datetime, UTC
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
        backup_base_path = "/tmp/backup"
        influx_url = os.environ.get("INFLUXDB_URL")
        influx_token_secret_arn = os.environ.get("INFLUXDB_TOKEN")
        influx_org = os.environ.get("INFLUXDB_ORG")                         # Not used in backup but included
        s3_bucket = os.environ.get("S3_BUCKET")

        # Validate environment variables
        if not all([influx_url, influx_token_secret_arn, s3_bucket]):
            raise ValueError("Missing required environment variables: INFLUXDB_URL, INFLUXDB_TOKEN, or S3_BUCKET")

        # Hardcode bucket names
        buckets = ["asset_bucket", "cloud_bucket"]

        # Fetch InfluxDB token
        influx_token = get_influx_token(influx_token_secret_arn)

        # S3 configuration
        timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
        s3_base_prefix = event.get("s3_prefix", f"influx-backups/monthly/{timestamp}/")
        s3_client = boto3.client("s3")

        # Create base backup directory
        os.makedirs(backup_base_path, exist_ok=True)

        # Backup each bucket
        for bucket_name in buckets:
            backup_path = f"{backup_base_path}/{bucket_name}"
            os.makedirs(backup_path, exist_ok=True)
            s3_prefix = f"{s3_base_prefix}{bucket_name}/"

            # Run backup command
            logger.info(f"Starting backup for bucket {bucket_name} to {backup_path}")
            cmd = [
                influx_bin, "backup", backup_path,
                "--host", influx_url,
                "--token", influx_token,
                "--bucket", bucket_name
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)  # 10-minute timeout
            if result.returncode != 0:
                logger.error(f"Backup failed for {bucket_name}: {result.stderr}")
                raise Exception(f"Backup failed: {result.stderr}")

            # Upload backup files to S3
            logger.info(f"Uploading backup files for {bucket_name} to s3://{s3_bucket}/{s3_prefix}")
            for file_name in os.listdir(backup_path):
                file_path = os.path.join(backup_path, file_name)
                s3_key = f"{s3_prefix}{file_name}"
                try:
                    with open(file_path, "rb") as f:
                        s3_client.upload_fileobj(
                            Fileobj=io.BufferedReader(f),
                            Bucket=s3_bucket,
                            Key=s3_key
                        )
                    logger.info(f"Uploaded {file_name} to {s3_key}")
                except ClientError as e:
                    logger.error(f"Failed to upload {file_name}: {str(e)}")
                    raise
                finally:
                    try:
                        os.remove(file_path)
                        logger.debug(f"Deleted local file {file_path}")
                    except OSError as e:
                        logger.warning(f"Failed to delete {file_path}: {str(e)}")

            # Clean up bucket-specific backup directory
            try:
                os.rmdir(backup_path)
                logger.debug(f"Deleted backup directory {backup_path}")
            except OSError as e:
                logger.warning(f"Failed to delete directory {backup_path}: {str(e)}")
            

        # Clean up base backup directory
        try:
            os.rmdir(backup_base_path)
            logger.debug(f"Deleted base backup directory {backup_base_path}")
        except OSError as e:
            logger.warning(f"Failed to delete base directory {backup_base_path}: {str(e)}")

        logger.info(f"Backup completed successfully for buckets {buckets} to s3://{s3_bucket}/{s3_base_prefix}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Backup completed for buckets {buckets} to s3://{s3_bucket}/{s3_base_prefix}",
                "buckets": buckets
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
