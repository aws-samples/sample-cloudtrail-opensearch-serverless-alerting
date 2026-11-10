# CloudTrail log analytics and alerting with Amazon OpenSearch Serverless

Companion code for the AWS Big Data Blog post *CloudTrail log analytics and alerting with OpenSearch Serverless*.

AWS CloudTrail logs flow from Amazon S3 through Amazon SQS and Amazon OpenSearch Ingestion into an
Amazon OpenSearch Serverless time series collection. An Amazon EventBridge schedule runs an Amazon ECS
task on AWS Fargate (Graviton/ARM64) that queries the collection with PPL and publishes matches to Amazon SNS.

The pipeline stores `requestParameters`, `responseElements`, `additionalEventData`, and `serviceEventDetails`
as JSON strings. Their shape differs for every API call, so mapping them as objects would exceed the index's
field limit and reject documents. Date detection is off, and only `@timestamp` and `eventTime` are mapped as
dates. Documents the collection still rejects go to a private S3 bucket (`PipelineDlqBucketName` output).
That way one bad record doesn't make SQS redeliver its whole log file, which would duplicate the file's other
records.

The collection is private: its network policies allow only the stack's OpenSearch Serverless VPC endpoint
and the endpoint OpenSearch Ingestion creates for the pipeline. The stack creates the pipeline's network
policy (`cloudtrail-logs-osi-net`), and the pipeline adds its endpoint to it when it starts. The stack creates its own VPC. The detection
task runs in private subnets and reaches AWS services through VPC endpoints (OpenSearch Serverless, ECR,
CloudWatch Logs, SNS, and an S3 gateway endpoint). A NAT gateway handles any other outbound traffic.

## Repository layout

| Path | Contents |
|---|---|
| `templates/cloudtrail-aoss-alerting.yaml` | CloudFormation template for the full stack |
| `cloudtrail-alert/` | Detection container: `app.py` (scheduled detection), `query.py` (ad-hoc PPL), `Dockerfile`, `requirements.txt` |
| `pipeline/cloudtrail-pipeline.yaml` | OpenSearch Ingestion pipeline configuration (for manual setup) |

## Prerequisites

- An AWS account and a CloudTrail trail that delivers logs to an S3 bucket
- AWS CLI v2 and Docker

The NAT gateway, its Elastic IP, and the interface endpoints (four services, two AZs each, plus the
OpenSearch Serverless endpoint) are billed per hour. The queue's customer managed KMS key and ECS Container
Insights add small monthly charges.

## Deploy

1. Put your values in a local `.env` file. It's git-ignored, so your bucket name, ARN, and email
   address stay out of the repository:

   ```bash
   cp .env.example .env    # then edit CLOUDTRAIL_BUCKET, ADMIN_PRINCIPAL_ARN, ALERT_EMAIL
   set -a; source .env; set +a
   ```

2. Deploy the stack with the schedule disabled:

   ```bash
   aws cloudformation deploy \
     --template-file templates/cloudtrail-aoss-alerting.yaml \
     --stack-name cloudtrail-aoss-alerting \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides \
       CloudTrailBucketName="$CLOUDTRAIL_BUCKET" \
       VpcCidr=10.20.0.0/16 \
       Generation=CLASSIC \
       AdminPrincipalArn="$ADMIN_PRINCIPAL_ARN" \
       AlertEmail="$ALERT_EMAIL"
   ```

   Set `Generation=NEXTGEN` to create a NextGen collection group whose capacity can scale to zero.

3. Run the command in the `NextStepS3Notification` stack output. It sends new CloudTrail log files
   (not digest files) to the queue. It replaces any existing notification configuration on the bucket.

4. Build and push the detection image (ARM64):

   ```bash
   REPO=$(aws cloudformation describe-stacks --stack-name cloudtrail-aoss-alerting \
     --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
   aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
   docker build --platform linux/arm64 -t "$REPO:1.0.0" cloudtrail-alert/
   docker push "$REPO:1.0.0"
   ```

   The repository's tags are immutable. `1.0.0` matches the default `ImageTag` stack parameter. To ship a
   new build, push it under a new tag and redeploy with `ImageTag=<new tag>`.

5. Enable the schedule and confirm the SNS email subscription:

   ```bash
   aws cloudformation deploy --template-file templates/cloudtrail-aoss-alerting.yaml \
     --stack-name cloudtrail-aoss-alerting --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides ScheduleState=ENABLED
   ```

## Test the detection task

Run the task once without waiting for the schedule, then check `/ecs/cloudtrail-alert` in CloudWatch Logs.
Exit code 0 means success; 1 means the query or publish failed.

```bash
out() { aws cloudformation describe-stacks --stack-name cloudtrail-aoss-alerting \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
NET="awsvpcConfiguration={subnets=[$(out PrivateSubnetIds)],securityGroups=[$(out TaskSecurityGroupId)],assignPublicIp=DISABLED}"
aws ecs run-task --cluster cloudtrail-alert --task-definition cloudtrail-alert --launch-type FARGATE \
  --network-configuration "$NET"
```

A successful run logs `Query returned N event(s)` and, when N > 0, `Published alert to <topic ARN>`.

## Query with PPL

The collection is VPC-only, so requests from outside the VPC are rejected, even when they're signed with
`AdminPrincipalArn` credentials (HTTP 401). To run ad-hoc PPL queries, start a one-off task that overrides the
container command with `query.py`. It reuses the detection task's read-only role and prints each row to
CloudWatch Logs as a JSON line.

```bash
# Uses out() and NET from the previous section.
ppl() {
  local overrides task
  overrides=$(jq -cn --arg q "$1" \
    '{containerOverrides:[{name:"cloudtrail-alert",command:["python","query.py",$q]}]}')
  task=$(aws ecs run-task --cluster cloudtrail-alert --task-definition cloudtrail-alert \
    --launch-type FARGATE --network-configuration "$NET" --overrides "$overrides" \
    --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster cloudtrail-alert --tasks "$task"
  aws logs get-log-events --log-group-name /ecs/cloudtrail-alert \
    --log-stream-name "ecs/cloudtrail-alert/${task##*/}" --query 'events[].message' --output text | tr '\t' '\n'
}
```

Example queries for Create events:

```bash
# Create events by service and API call
ppl "source=\`cloudtrail-logs\` | where like(eventName, '%Create%') | stats count() as events by eventSource, eventName | sort - events"

# 20 most recent Create events and who made them
ppl "source=\`cloudtrail-logs\` | where like(eventName, '%Create%') | fields eventTime, eventName, eventSource, awsRegion, userIdentity.arn | sort - eventTime | head 20"

# Create events in the last 24 hours, grouped by caller
ppl "source=\`cloudtrail-logs\` | where like(eventName, '%Create%') and @timestamp > DATE_SUB(NOW(), INTERVAL 1 DAY) | stats count() as events by userIdentity.arn | sort - events"
```

The first query returns rows like:

```json
{"events": 5, "eventSource": "ec2.amazonaws.com", "eventName": "CreateSubnet"}
{"events": 5, "eventSource": "ec2.amazonaws.com", "eventName": "CreateVpcEndpoint"}
{"events": 4, "eventSource": "route53.amazonaws.com", "eventName": "CreateHostedZone"}
```

Exit code 0 means the query ran; 1 means it failed (the error is in the log stream).

`requestParameters` and `responseElements` are analyzed text fields, so search inside them with the
full-text `match()` function. `like()` wildcards don't match within analyzed text.

```bash
ppl "source=\`cloudtrail-logs\` | where eventName = 'CreateSubnet' and match(requestParameters, '10.20.2.0') | fields eventTime, requestParameters"
```

## Monitor ingestion

- `DeadLetterQueueUrl`: S3 notifications for log files that failed 5 times. Alarm on its depth.
- `PipelineDlqBucketName`: individual documents the collection rejected, under `opensearch-sink-dlq/`.
- `/aws/vendedlogs/OpenSearchIngestion/cloudtrail-pipeline`: pipeline logs. A healthy pipeline logs no
  `BulkRetryStrategy` warnings.

For OpenSearch Dashboards or interactive queries, use a host inside the VPC, such as an instance in a
private subnet that you reach through AWS Systems Manager Session Manager. Allow that host's security group
in the ingress rule of the endpoint security group.

## Clean up

```bash
aws s3api put-bucket-notification-configuration --bucket "$CLOUDTRAIL_BUCKET" --notification-configuration '{}'
aws cloudformation delete-stack --stack-name cloudtrail-aoss-alerting
aws cloudformation wait stack-delete-complete --stack-name cloudtrail-aoss-alerting
```

The first command removes every notification on the bucket; edit the configuration instead if the bucket has others.
The pipeline DLQ bucket and its access-logs bucket are retained when the stack is deleted, so rejected records
and their audit trail aren't lost. Delete them yourself once you no longer need them.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
