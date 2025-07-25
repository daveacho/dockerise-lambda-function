import subprocess
import boto3
import os
import json
import logging
import gzip
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
        temp_dir = "/tmp/restore"
        influx_url = os.environ.get("INFLUXDB_URL")
        influx_token_secret_arn = os.environ.get("INFLUXDB_TOKEN")
        influx_org = os.environ.get("INFLUXDB_ORG")   #change to destination_org
        s3_bucket = os.environ.get("S3_BUCKET")

        # Validate environment variables
        if not all([influx_url, influx_token_secret_arn, influx_org, s3_bucket]):
            raise ValueError("Missing required environment variables: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, or S3_BUCKET")

        # Get backup date from event (format: YYYY-MM-DD)
        backup_date = event.get("backup_date")
        if not backup_date:
            raise ValueError("Missing 'backup_date' in event (format: YYYY-MM-DD)")
        try:
            datetime.strptime(backup_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid 'backup_date' format, expected YYYY-MM-DD")

        # Default bucket configuration
        buckets = [
            {"name": "asset_bucket", "s3_path": f"influx-backups/daily/{backup_date}/asset_bucket/", "dest_bucket": "restored_asset_bucket"},
            {"name": "cloud_bucket", "s3_path": f"influx-backups/daily/{backup_date}/cloud_bucket/", "dest_bucket": "restored_cloud_bucket"}
        ]

        # Fetch InfluxDB token
        influx_token = get_influx_token(influx_token_secret_arn)

        # S3 configuration
        s3_client = boto3.client("s3")

        # Create temporary directory
        logger.info(f"Creating temporary directory {temp_dir}")
        os.makedirs(temp_dir, exist_ok=True)

        # Track successful and failed restorations
        restored_buckets = []
        failed_buckets = []

        # Restore CSVs for each bucket
        for bucket in buckets:
            bucket_name = bucket["name"]
            dest_bucket = bucket["dest_bucket"]
            s3_prefix = bucket["s3_path"]
            csv_filename = f"data-{backup_date}.csv.gz"
            s3_key = f"{s3_prefix}{csv_filename}"
            temp_gzip = f"{temp_dir}/{bucket_name}_data.csv.gz"
            temp_csv = f"{temp_dir}/{bucket_name}_data.csv"

            try:
                # Download CSV from S3
                logger.info(f"Downloading s3://{s3_bucket}/{s3_key} to {temp_gzip}")
                s3_client.download_file(Bucket=s3_bucket, Key=s3_key, Filename=temp_gzip)

                # Decompress gzip
                logger.info(f"Decompressing {temp_gzip} to {temp_csv}")
                with gzip.open(temp_gzip, "rb") as f_in, open(temp_csv, "wb") as f_out:
                    f_out.write(f_in.read())
                try:
                    os.remove(temp_gzip)
                    logger.debug(f"Removed temporary gzip file {temp_gzip}")
                except OSError as e:
                    logger.warning(f"Failed to remove {temp_gzip}: {str(e)}")

                # Restore CSV using influx write
                logger.info(f"Restoring {temp_csv} to bucket {dest_bucket}")
                cmd = [
                    influx_bin, "write",
                    "--host", influx_url,
                    "--org", influx_org,
                    "--token", influx_token,
                    "--bucket", dest_bucket,
                    "--format", "csv",
                    "--file", temp_csv
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300  # 5-minute timeout
                )
                if result.returncode != 0:
                    logger.error(f"Restore failed for {bucket_name}: {result.stderr}")
                    raise Exception(f"Restore failed for {bucket_name}: {result.stderr}")

                # Clean up temporary CSV
                try:
                    os.remove(temp_csv)
                    logger.debug(f"Removed temporary CSV file {temp_csv}")
                except OSError as e:
                    logger.warning(f"Failed to remove {temp_csv}: {str(e)}")

                logger.info(f"Restored {csv_filename} for {bucket_name} to {dest_bucket}")
                restored_buckets.append(bucket_name)

            except s3_client.exceptions.ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    logger.warning(f"No backup found for {bucket_name} on {backup_date}")
                    failed_buckets.append(bucket_name)
                    continue
                logger.error(f"Failed to download {s3_key} for {bucket_name}: {str(e)}")
                failed_buckets.append(bucket_name)
                continue
            except Exception as e:
                logger.error(f"Failed to restore {bucket_name}: {str(e)}")
                failed_buckets.append(bucket_name)
                continue

        # Clean up temporary directory
        try:
            os.rmdir(temp_dir)
            logger.info(f"Removed temporary directory {temp_dir}")
        except OSError as e:
            logger.warning(f"Failed to remove {temp_dir}: {str(e)}")

        # Log and return results
        if not restored_buckets and failed_buckets:
            logger.error(f"Restoration failed for all buckets: {failed_buckets}")
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "error": f"Restoration failed for all buckets: {failed_buckets}",
                    "failed_buckets": failed_buckets
                })
            }

        logger.info(f"Restoration completed for {backup_date} to bucket {dest_bucket}, restored: {restored_buckets}, failed: {failed_buckets}")
        return {
            "statusCode": 200 if restored_buckets else 500,
            "body": json.dumps({
                "message": f"Restoration completed for {backup_date} to bucket {dest_bucket}",
                "restored_buckets": restored_buckets,
                "failed_buckets": failed_buckets
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