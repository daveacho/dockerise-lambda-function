FROM public.ecr.aws/lambda/python:3.11

# Copy requirements and install Python dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}

RUN pip install -r requirements.txt

# Copy function code
COPY *.py ${LAMBDA_TASK_ROOT}

# Set the CMD to your handler (this will be overridden by Lambda)
CMD ["cognito_backup.lambda_handler"]

