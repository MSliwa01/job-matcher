# The demo on AWS Lambda behind a function URL. The image is the same one Azure runs; the Lambda Web
# Adapter extension inside it turns invocations into HTTP requests to uvicorn. Lambda's always-free tier
# (1 million requests, 400,000 GB-seconds a month) covers a demo. ECR storage for two ~530 MB images costs
# about $0.10 a month.
#
# Two steps, because Lambda needs the image in ECR before it can be created:
#   terraform apply -target=aws_ecr_repository.repo
#   ../push.sh aws <tag>
#   terraform apply -var image_tag=<tag>

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }
}

provider "aws" {
  region = var.region
}

resource "aws_ecr_repository" "repo" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_lifecycle_policy" "keep_two" {
  repository = aws_ecr_repository.repo.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the two newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 2 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_iam_role" "lambda" {
  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "logs" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 7
}

resource "aws_lambda_function" "api" {
  function_name = var.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.repo.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  memory_size   = 2048 # the int8 model and the snapshot take about 1.1 GB; memory also buys CPU
  timeout       = 60   # the first request after a cold start loads the model
  # A ceiling on the bill if someone floods the URL. Accounts with the default quota of 10 concurrent
  # executions cannot reserve any; set reserved_concurrency = -1 there.
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = { AWS_LWA_READINESS_CHECK_PATH = "/healthz" }
  }

  depends_on = [aws_cloudwatch_log_group.logs, aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_url" "url" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"
}

# A public function URL needs both permissions since October 2025.
resource "aws_lambda_permission" "url" {
  statement_id           = "public-function-url"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "invoke" {
  statement_id             = "public-invoke-via-url"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.api.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# Cost guard. Budgets only send alerts, so a small function stops the API when the budget is spent:
# an email at 40% of the budget and on a forecast above it, the kill switch at 100% of actual cost.
# AWS updates cost data a few times a day, so the stop comes hours after the money is spent, not seconds.
resource "aws_sns_topic" "budget" {
  name = "${var.name}-budget"
}

resource "aws_sns_topic_policy" "budget" {
  arn = aws_sns_topic.budget.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.budget.arn
    }]
  })
}

data "archive_file" "kill" {
  type        = "zip"
  source_file = "${path.module}/kill.py"
  output_path = "${path.module}/.build/kill.zip"
}

resource "aws_iam_role" "kill" {
  name               = "${var.name}-kill"
  assume_role_policy = aws_iam_role.lambda.assume_role_policy
}

resource "aws_iam_role_policy_attachment" "kill_logs" {
  role       = aws_iam_role.kill.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "kill" {
  role = aws_iam_role.kill.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:PutFunctionConcurrency", Resource = aws_lambda_function.api.arn }]
  })
}

resource "aws_lambda_function" "kill" {
  function_name    = "${var.name}-kill"
  role             = aws_iam_role.kill.arn
  runtime          = "python3.13"
  handler          = "kill.handler"
  filename         = data.archive_file.kill.output_path
  source_code_hash = data.archive_file.kill.output_base64sha256
  timeout          = 30
  environment {
    variables = { FUNCTION_NAME = aws_lambda_function.api.function_name }
  }
}

resource "aws_sns_topic_subscription" "kill" {
  topic_arn = aws_sns_topic.budget.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.kill.arn
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "budget-topic"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.kill.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.budget.arn
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 40
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
    subscriber_sns_topic_arns  = [aws_sns_topic.budget.arn]
  }

  depends_on = [aws_sns_topic_policy.budget]
}

output "repository_url" {
  value = aws_ecr_repository.repo.repository_url
}

output "url" {
  value = aws_lambda_function_url.url.function_url
}
