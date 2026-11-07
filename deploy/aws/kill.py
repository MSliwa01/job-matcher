"""Budget kill switch: when the monthly budget is exceeded, AWS Budgets publishes to SNS and this function
sets the API's reserved concurrency to 0, so the function URL answers 429 and nothing more is billed.
`terraform apply` restores the normal concurrency."""
import os

import boto3


def handler(event, context):
    name = os.environ["FUNCTION_NAME"]
    boto3.client("lambda").put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=0)
    print(f"stopped {name}: budget exceeded")
    return {"stopped": name}
