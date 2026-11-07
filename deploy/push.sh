#!/usr/bin/env bash
# Build the demo image from a fresh snapshot and push it.
#   ./deploy/push.sh ghcr <tag>   ghcr.io/<github user>/job-matcher-demo:<tag>, public, for Azure
#   ./deploy/push.sh aws <tag>    the ECR repository from deploy/aws, for Lambda
set -euo pipefail
target=${1:?ghcr or aws}
tag=${2:-$(date +%F)}
cd "$(dirname "$0")/.."

uv run jm snapshot snapshot
docker build --platform linux/amd64 --provenance=false -t job-matcher-demo:"$tag" .

case "$target" in
  ghcr)
    user=$(gh api user --jq .login | tr '[:upper:]' '[:lower:]')
    gh auth token | docker login ghcr.io -u "$user" --password-stdin
    ref=ghcr.io/$user/job-matcher-demo:$tag
    ;;
  aws)
    repo=$(terraform -chdir=deploy/aws output -raw repository_url)
    aws ecr get-login-password --region "${AWS_REGION:-eu-central-1}" \
      | docker login --username AWS --password-stdin "${repo%%/*}"
    ref=$repo:$tag
    ;;
  *) echo "unknown target $target" >&2; exit 1 ;;
esac
docker tag job-matcher-demo:"$tag" "$ref"
docker push "$ref"
echo "$ref"
