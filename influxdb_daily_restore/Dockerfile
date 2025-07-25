#AWS Lambda base image
FROM public.ecr.aws/lambda/python:3.12-arm64

#Set working directory
WORKDIR /usr/app

#Install dependencies first (optimized for caching)
COPY requirements.txt /usr/app

RUN pip install --no-cache-dir -r requirements.txt

#copy the rest of the application files
COPY influxdb_daily_restore.py /usr/app

#start the lambda function
CMD ["influxdb_daily_restore.lambda_handler"]