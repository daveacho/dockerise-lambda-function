import subprocess
import boto3
import os
import json
import logging
import io
import gzip
from datetime import datetime, UTC, timedelta
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

def get_last_backup_time(s3_client, s3_bucket, influx_bucket):
    """Retrieve last backup timestamp from S3 for a specific bucket."""
    last_backup_key = f"influx-backups/last_incremental_timestamp_{influx_bucket}.json"
    try:
        response = s3_client.get_object(Bucket=s3_bucket, Key=last_backup_key)
        data = json.loads(response["Body"].read().decode("utf-8"))
        return datetime.fromisoformat(data["last_backup_time"])
    except s3_client.exceptions.NoSuchKey:
        logger.info(f"No previous backup timestamp found for {influx_bucket}, defaulting to 24 hours ago")
        return datetime.now(UTC) - timedelta(hours=24)
    except ClientError as e:
        logger.error(f"Failed to retrieve last backup timestamp for {influx_bucket}: {str(e)}")
        raise

def update_last_backup_time(s3_client, s3_bucket, timestamp, influx_bucket):
    """Update last backup timestamp in S3 for a specific bucket."""
    last_backup_key = f"influx-backups/last_incremental_timestamp_{influx_bucket}.json"
    try:
        data = {"last_backup_time": timestamp.isoformat()}
        s3_client.put_object(Bucket=s3_bucket, Key=last_backup_key, Body=json.dumps(data))
        logger.info(f"Updated last backup timestamp for {influx_bucket}")
    except ClientError as e:
        logger.error(f"Failed to update last backup timestamp for {influx_bucket}: {str(e)}")
        raise

def lambda_handler(event, context):
    try:
        # Initialize variables from environment
        influx_bin = "/opt/bin/influx"
        temp_csv = "/tmp/data.csv"
        temp_gzip = "/tmp/data.csv.gz"
        influx_url = os.environ.get("INFLUXDB_URL")
        influx_token_secret_arn = os.environ.get("INFLUXDB_TOKEN")
        influx_org = os.environ.get("INFLUXDB_ORG")
        s3_bucket = os.environ.get("S3_BUCKET")
        bucket_config_env = os.environ.get("INFLUXDB_BUCKET_CONFIG", "")

        # Validate environment variables
        if not all([influx_url, influx_token_secret_arn, influx_org, s3_bucket]):
            raise ValueError("Missing required environment variables: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, or S3_BUCKET")

        # Default bucket configuration
        default_buckets = [
            {"name": "asset_bucket", "measurements": ["cloud_telemetry", "telemetry"]},
            {"name": "cloud_bucket", "measurements": ["savings"]}
        ]

        # Load bucket configurations from event or environment
        buckets = event.get("buckets", [])
        if not buckets:
            if bucket_config_env:
                try:
                    buckets = json.loads(bucket_config_env)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid INFLUXDB_BUCKET_CONFIG JSON: {str(e)}, using default buckets")
                    buckets = default_buckets
            else:
                buckets = default_buckets

        # Validate bucket configurations
        for bucket in buckets:
            if not isinstance(bucket, dict) or "name" not in bucket or "measurements" not in bucket:
                raise ValueError(f"Invalid bucket configuration: {bucket}, expected {'name': str, 'measurements': list}")
            bucket["name"] = bucket["name"].strip()
            bucket["measurements"] = [m.strip() for m in bucket["measurements"] if m.strip()]

        # Fetch InfluxDB token
        influx_token = get_influx_token(influx_token_secret_arn)

        # S3 configuration
        s3_client = boto3.client("s3")
        current_time = datetime.now(UTC)
        daily_prefix = current_time.strftime('%Y-%m-%d')

        # Track successful backups
        backed_up_buckets = []

        # Process each bucket
        for bucket in buckets:
            influx_bucket = bucket["name"]
            measurements = bucket["measurements"]
            if not measurements:
                logger.warning(f"No measurements specified for {influx_bucket}, skipping")
                continue

            logger.info(f"Starting incremental backup for bucket {influx_bucket} with measurements {measurements}")

            # S3 prefix for this bucket
            s3_prefix = f"influx-backups/daily/{daily_prefix}/{influx_bucket}/"
            csv_filename = f"data-{current_time.strftime('%Y-%m-%d')}.csv.gz"
            s3_key = f"{s3_prefix}{csv_filename}"

            # Get last backup time
            start_time = get_last_backup_time(s3_client, s3_bucket, influx_bucket)
            stop_time = current_time
            start_time_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')
            stop_time_str = stop_time.strftime('%Y-%m-%dT%H:%M:%SZ')

            # Build query command with dynamic measurements
            measurement_filters = " or ".join([f'r._measurement == "{m}"' for m in measurements])
            query = (
                f'from(bucket: "{influx_bucket}") '
                f'|> range(start: {start_time_str}, stop: {stop_time_str}) '
                f'|> filter(fn: (r) => {measurement_filters})'
            )
            cmd = [
                influx_bin, "query", query,
                "--host", influx_url,
                "--org", influx_org,
                "--token", influx_token,
                "--raw"
            ]
            logger.info(f"Executing influx query command for {influx_bucket}: {' '.join(cmd)}")

            # Run query and save to temporary CSV
            logger.info(f"Querying data from {influx_bucket} to {temp_csv}")
            with open(temp_csv, "w") as f:
                result = subprocess.run(
                    cmd,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300  # 5-minute timeout
                )
                if result.returncode != 0:
                    logger.error(f"Query failed for {influx_bucket}: {result.stderr}")
                    raise Exception(f"Query failed for {influx_bucket}: {result.stderr}")

            # Check if CSV is empty
            if os.path.getsize(temp_csv) == 0:
                logger.info(f"No data returned for {influx_bucket} for the period")
                try:
                    os.remove(temp_csv)
                    logger.debug(f"Removed empty temporary file {temp_csv}")
                except OSError as e:
                    logger.warning(f"Failed to remove {temp_csv}: {str(e)}")
                update_last_backup_time(s3_client, s3_bucket, current_time, influx_bucket)
                backed_up_buckets.append(influx_bucket)
                continue

            # Compress CSV to gzip
            logger.info(f"Compressing {temp_csv} to {temp_gzip}")
            with open(temp_csv, "rb") as f_in, gzip.open(temp_gzip, "wb") as f_out:
                f_out.writelines(f_in)
            try:
                os.remove(temp_csv)
                logger.debug(f"Removed temporary CSV file {temp_csv}")
            except OSError as e:
                logger.warning(f"Failed to remove {temp_csv}: {str(e)}")

            # Upload compressed CSV to S3
            logger.info(f"Uploading {csv_filename} to s3://{s3_bucket}/{s3_key}")
            try:
                with open(temp_gzip, "rb") as f:
                    s3_client.upload_fileobj(
                        Fileobj=io.BufferedReader(f),
                        Bucket=s3_bucket,
                        Key=s3_key
                    )
                logger.info(f"Uploaded {csv_filename} to {s3_key}")
            except ClientError as e:
                logger.error(f"Failed to upload {csv_filename} for {influx_bucket}: {str(e)}")
                raise
            finally:
                try:
                    os.remove(temp_gzip)
                    logger.debug(f"Removed temporary gzip file {temp_gzip}")
                except OSError as e:
                    logger.warning(f"Failed to remove {temp_gzip}: {str(e)}")

            # Update last backup timestamp
            update_last_backup_time(s3_client, s3_bucket, current_time, influx_bucket)
            backed_up_buckets.append(influx_bucket)
            logger.info(f"Incremental backup completed for {influx_bucket} to s3://{s3_bucket}/{s3_key}")

        logger.info(f"Incremental backup completed for buckets {backed_up_buckets}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Incremental backup completed for buckets {backed_up_buckets}",
                "buckets": backed_up_buckets
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