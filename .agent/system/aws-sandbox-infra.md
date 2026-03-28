# AWS Sandbox Infrastructure for Pilot

## Architecture

```
Pilot Orchestrator (local or EC2)
  │
  ├── Create ECS Task (per ticket)
  │     └── Fargate container (Firecracker microVM)
  │           ├── Golden AMI / Docker image with everything pre-installed
  │           ├── pilot binary + CC 2.1.74 + Node.js + numpy + uv
  │           └── Task executes → PR created → container dies
  │
  ├── Or: Warm Pool EC2 (for bench)
  │     └── Pre-booted instances, ~5s resume
  │
  └── Results → S3 bucket (pattern DB, logs, artifacts)
```

## CloudFormation Stack

### 1. ECR Repository (Docker Image)

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Pilot Agent Sandbox Infrastructure

Parameters:
  Environment:
    Type: String
    Default: dev
    AllowedValues: [dev, staging, prod]

Resources:
  # Container registry for pre-built agent image
  PilotAgentRepo:
    Type: AWS::ECR::Repository
    Properties:
      RepositoryName: !Sub pilot-agent-${Environment}
      ImageScanningConfiguration:
        ScanOnPush: true
      LifecyclePolicy:
        LifecyclePolicyText: |
          {
            "rules": [
              {
                "rulePriority": 1,
                "selection": {
                  "tagStatus": "untagged",
                  "countType": "sinceImagePushed",
                  "countUnit": "days",
                  "countNumber": 7
                },
                "action": { "type": "expire" }
              }
            ]
          }
```

### 2. ECS Cluster + Task Definition

```yaml
  PilotCluster:
    Type: AWS::ECS::Cluster
    Properties:
      ClusterName: !Sub pilot-${Environment}
      CapacityProviders:
        - FARGATE
        - FARGATE_SPOT  # 70% cheaper for bench runs
      DefaultCapacityProviderStrategy:
        - CapacityProvider: FARGATE_SPOT
          Weight: 1

  # Task execution role (pull images, write logs)
  TaskExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: ecs-tasks.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

  # Task role (what the agent can access)
  TaskRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: ecs-tasks.amazonaws.com
            Action: sts:AssumeRole
      Policies:
        - PolicyName: PilotAgentPolicy
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              # Access to pattern DB + artifacts bucket
              - Effect: Allow
                Action:
                  - s3:GetObject
                  - s3:PutObject
                Resource: !Sub arn:aws:s3:::pilot-data-${Environment}/*
              # No other permissions — sandboxed

  PilotTaskDef:
    Type: AWS::ECS::TaskDefinition
    Properties:
      Family: !Sub pilot-agent-${Environment}
      Cpu: '2048'       # 2 vCPU
      Memory: '4096'    # 4 GB (matches Terminal Bench containers)
      NetworkMode: awsvpc
      RequiresCompatibilities:
        - FARGATE
      ExecutionRoleArn: !GetAtt TaskExecutionRole.Arn
      TaskRoleArn: !GetAtt TaskRole.Arn
      ContainerDefinitions:
        - Name: pilot-agent
          Image: !Sub ${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/pilot-agent-${Environment}:latest
          Essential: true
          # Environment injected at runtime by Pilot orchestrator
          Environment:
            - Name: IS_SANDBOX
              Value: '1'
          LogConfiguration:
            LogDriver: awslogs
            Options:
              awslogs-group: !Ref LogGroup
              awslogs-region: !Ref AWS::Region
              awslogs-stream-prefix: pilot
          # No port mappings — agent doesn't serve traffic

  LogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub /ecs/pilot-agent-${Environment}
      RetentionInDays: 14
```

### 3. S3 Bucket for Pattern DB + Artifacts

```yaml
  DataBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub pilot-data-${Environment}
      VersioningConfiguration:
        Status: Enabled
      LifecycleConfiguration:
        Rules:
          - Id: CleanupOldArtifacts
            Status: Enabled
            ExpirationInDays: 30
            Prefix: artifacts/
          # Pattern DB never expires
```

### 4. VPC + Security (Sandboxing)

```yaml
  SandboxVPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
      EnableDnsSupport: true
      EnableDnsHostnames: true

  PrivateSubnet1:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref SandboxVPC
      CidrBlock: 10.0.1.0/24
      AvailabilityZone: !Select [0, !GetAZs '']

  PrivateSubnet2:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref SandboxVPC
      CidrBlock: 10.0.2.0/24
      AvailabilityZone: !Select [1, !GetAZs '']

  # NAT Gateway for outbound internet (pip install, API calls)
  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !Ref PrivateSubnet1
      AllocationId: !GetAtt NatEIP.AllocationId

  NatEIP:
    Type: AWS::EC2::EIP

  # Security group — outbound only, no inbound
  AgentSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Pilot agent sandbox - outbound only
      VpcId: !Ref SandboxVPC
      SecurityGroupEgress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 0.0.0.0/0  # HTTPS for API calls
        - IpProtocol: tcp
          FromPort: 80
          ToPort: 80
          CidrIp: 0.0.0.0/0  # HTTP for package installs
      # No ingress rules — fully sandboxed
```

## Dockerfile (Golden Image)

```dockerfile
FROM debian:bookworm-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git jq gcc g++ make python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Node.js + Claude Code (pinned)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code@2.1.74

# uv/uvx
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /root/.local/bin/uvx /usr/local/bin/uvx

# Python deps
RUN pip install --break-system-packages numpy

# Pilot binary (pre-built, stripped)
COPY pilot-linux-amd64 /usr/local/bin/pilot
RUN chmod +x /usr/local/bin/pilot

# Pattern DB seed
COPY pilot.db /root/.pilot/data/pilot.db

# Working directory
WORKDIR /app
RUN git config --global init.defaultBranch main \
    && git config --global user.email "pilot@quantflow.studio" \
    && git config --global user.name "Pilot"

# Logs directory
RUN mkdir -p /logs/agent
```

## How Pilot Orchestrator Runs Tasks

```python
# In Pilot's executor (Go or Python):

import boto3

ecs = boto3.client('ecs')

def run_task(instruction: str, project_path: str, oauth_token: str):
    response = ecs.run_task(
        cluster='pilot-dev',
        taskDefinition='pilot-agent-dev',
        launchType='FARGATE',
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': ['subnet-xxx', 'subnet-yyy'],
                'securityGroups': ['sg-xxx'],
                'assignPublicIp': 'DISABLED',
            }
        },
        overrides={
            'containerOverrides': [{
                'name': 'pilot-agent',
                'command': [
                    'pilot', 'task', instruction,
                    '--local', '--project', '/app',
                    '--result-json', '/logs/agent/pilot-result.json',
                ],
                'environment': [
                    {'name': 'CLAUDE_CODE_OAUTH_TOKEN', 'value': oauth_token},
                ],
            }],
        },
    )
    return response['tasks'][0]['taskArn']
```

## Cost Comparison

| Infra | Per Task (2 vCPU, 4GB, 30 min) | Setup Time | Notes |
|-------|-------------------------------|------------|-------|
| Modal | ~$0.05 | 30s + 5-15 min upload | Binary upload hangs |
| Fargate | ~$0.03 | 30-60s (image cached) | No upload — image pre-built |
| Fargate Spot | ~$0.01 | 30-60s | 70% cheaper, can be interrupted |
| EC2 Warm Pool | ~$0.02 | 5-10s | Fastest, needs management |

**For 445 bench trials:**
- Modal: ~$22 + upload pain
- Fargate Spot: ~$4.50
- EC2 Warm Pool: ~$9 (+ idle cost)

## Migration Path

1. Build Dockerfile, push to ECR
2. Deploy CloudFormation stack
3. Add AWS environment plugin to Harbor (or use Pilot's orchestrator directly)
4. Run bench: same `harbor run` command, just `-e aws` instead of `-e modal`
5. Pattern DB synced via S3 instead of file upload/download
