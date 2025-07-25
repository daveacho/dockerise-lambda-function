#AWS Lambda base image
FROM public.ecr.aws/lambda/python:3.12-arm64

#Set working directory
WORKDIR /usr/app

#Install dependencies first (optimized for caching)
copy requirements.txt /usr/app

RUN pip install --no-cache-dir -r requirements.txt

#copy the rest of the application files
COPY influxdb-monthly-restore.py /usr/app

#start the lambda function
CMD ["influxdb-monthly-restore.lambda_handler"]