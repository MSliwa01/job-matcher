# The demo on AWS Lambda behind a function URL. The image is the same one Azure runs; the Lambda Web
# Adapter extension inside it turns invocations into HTTP requests to uvicorn. Lambda's always-free tier
# (1 million requests, 400,000 GB-seconds a month) covers a demo. ECR storage for one ~530 MB image costs
# about $0.05 a month.
#
# Two steps, because Lambda needs the image in ECR before it can be created:
#   terraform apply -target=aws_ecr_repository.repo
#   ../push.sh aws <tag>
#   terraform apply -var image_tag=<tag>

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
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

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
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
}

output "repository_url" {
  value = aws_ecr_repository.repo.repository_url
}

output "url" {
  value = aws_lambda_function_url.url.function_url
}
