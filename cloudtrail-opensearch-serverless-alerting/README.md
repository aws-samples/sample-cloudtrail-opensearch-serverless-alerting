# CloudTrail log analytics and alerting with Amazon OpenSearch Serverless

Companion code for the AWS Big Data Blog post *CloudTrail log analytics and alerting with OpenSearch Serverless*.

AWS CloudTrail logs flow from Amazon S3 through Amazon SQS and Amazon OpenSearch Ingestion into an
Amazon OpenSearch Serverless time series collection. An Amazon EventBridge schedule runs an Amazon ECS
task on AWS Fargate (Graviton/ARM64) that queries the collection with PPL and publishes matches to Amazon SNS.

## Repository layout

| Path | Contents |
|---|---|
| `templates/cloudtrail-aoss-alerting.yaml` | CloudFormation template for the full stack |
| `cloudtrail-alert/` | Detection container: `app.py`, `Dockerfile`, `requirements.txt` |
| `pipeline/cloudtrail-pipeline.yaml` | OpenSearch Ingestion pipeline configuration (for manual setup) |

## Prerequisites

- An AWS account and a CloudTrail trail that delivers logs to an S3 bucket
- AWS CLI v2 and Docker
- A VPC with two subnets for the Fargate task

## Deploy

1. Deploy the stack with the schedule disabled:

   ```bash
   aws cloudformation deploy \
     --template-file templates/cloudtrail-aoss-alerting.yaml \
     --stack-name cloudtrail-aoss-alerting \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides \
       CloudTrailBucketName=<YOUR_CLOUDTRAIL_BUCKET> \
       VpcId=<VPC_ID> SubnetIds=<SUBNET_ID_1>,<SUBNET_ID_2> \
       Generation=CLASSIC \
       AdminPrincipalArn=<YOUR_IAM_ROLE_ARN> \
       AlertEmail=ops@example.com
   ```

   Set `Generation=NEXTGEN` to create a NextGen collection group whose capacity can scale to zero.

2. Run the command in the `NextStepS3Notification` stack output. It sends new CloudTrail log files
   (not digest files) to the queue. It replaces any existing notification configuration on the bucket.

3. Build and push the detection image (ARM64):

   ```bash
   REPO=$(aws cloudformation describe-stacks --stack-name cloudtrail-aoss-alerting \
     --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
   aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
   docker build --platform linux/arm64 -t "$REPO:latest" cloudtrail-alert/
   docker push "$REPO:latest"
   ```

4. Enable the schedule and confirm the SNS email subscription:

   ```bash
   aws cloudformation deploy --template-file templates/cloudtrail-aoss-alerting.yaml \
     --stack-name cloudtrail-aoss-alerting --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides ScheduleState=ENABLED
   ```

## Test the detection task

Run the task once without waiting for the schedule, then check `/ecs/cloudtrail-alert` in CloudWatch Logs.
Exit code 0 means success; 1 means the query or publish failed.

## Clean up

```bash
aws s3api put-bucket-notification-configuration --bucket <YOUR_CLOUDTRAIL_BUCKET> --notification-configuration '{}'
aws cloudformation delete-stack --stack-name cloudtrail-aoss-alerting
```

The first command removes every notification on the bucket; edit the configuration instead if the bucket has others.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
