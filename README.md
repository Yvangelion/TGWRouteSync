# AWS Transit Gateway Automatic Route Sync

> Automatically synchronize AWS Transit Gateway routes to VPC subnet route tables using Network Manager events and EventBridge.

[![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-FF9900?style=flat&logo=awslambda&logoColor=white)](https://aws.amazon.com/lambda/)
[![Amazon EventBridge](https://img.shields.io/badge/AWS-EventBridge-FF4F8B?style=flat&logo=amazoneventbridge&logoColor=white)](https://aws.amazon.com/eventbridge/)
[![AWS IAM](https://img.shields.io/badge/AWS-IAM-232F3E?style=flat&logo=amazonaws&logoColor=white)](https://aws.amazon.com/iam/)
[![AWS Transit Gateway](https://img.shields.io/badge/AWS-Transit%20Gateway-FF9900?style=flat&logo=amazonaws&logoColor=white)](https://aws.amazon.com/transit-gateway/)
[![AWS Network Manager](https://img.shields.io/badge/AWS-Network%20Manager-FF9900?style=flat&logo=amazonaws&logoColor=white)](https://aws.amazon.com/cloud-wan/)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [How It Works](#-how-it-works)
- [Prerequisites](#-prerequisites)
- [Regional Requirements](#-regional-requirements)
- [Setup Guide](#-setup-guide)
  - [Step 1: Register TGW with Network Manager](#step-1-register-tgw-with-network-manager)
  - [Step 2: Create IAM Role](#step-2-create-iam-role)
  - [Step 3: Create Lambda Function](#step-3-create-lambda-function)
  - [Step 4: Create EventBridge Rule](#step-4-create-eventbridge-rule)
  - [Step 5: Tag VPC Route Tables](#step-5-tag-vpc-route-tables)
- [Configuration](#-configuration)
- [Testing](#-testing)
- [Troubleshooting](#-troubleshooting)
- [IAM Policy Reference](#-iam-policy-reference)
- [Contributing](#-contributing)
- [License](#-license)

---

## Overview

Managing route tables across multiple VPCs connected to a Transit Gateway can be complex and error-prone. This solution automates the synchronization process by:

- **Listening** to Network Manager routing events via EventBridge
- **Detecting** route changes in TGW route tables (static, propagated, VPN)
- **Syncing** routes to VPC subnet route tables tagged for synchronization
- **Supporting** multiple Transit Gateways across different AWS regions

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                    EVENT FLOW                                           │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   TRIGGER SOURCES (Any Region)                                                          │
│   ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐                  │
│   │  VPN Route       │    │  Static Route    │    │  Scheduled Poll  │                  │
│   │  Change (BGP)    │    │  Added/Removed   │    │  (Backup)        │                  │
│   └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘                  │
│            │                       │                       │                            │
│            └───────────────────────┼───────────────────────┘                            │
│                                    ▼                                                    │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐   │
│   │                      AWS NETWORK MANAGER (Global)                               │   │
│   │                   TGW must be registered here to emit events                    │   │
│   └─────────────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                                    │
│                                    ▼                                                    │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐   │
│   │                    AMAZON EVENTBRIDGE (us-west-2 only)                          │   │
│   │                                                                                 │   │
│   │   Rule filters: source = "aws.networkmanager"                                   │   │
│   │                 detail-type = "Network Manager Routing Update"                  │   │
│   └─────────────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                                    │
│                                    ▼                                                    │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐   │
│   │                    AWS LAMBDA (us-west-2, calls any region)                     │   │
│   │                                                                                 │   │
│   │   1. Extract TGW Route Table ID from event                                      │   │
│   │   2. Get all routes from TGW route table                                        │   │
│   │   3. Find VPC attachments associated with that TGW route table                  │   │
│   │   4. Find VPC route tables tagged: TGWRouteSync=enabled                         │   │
│   │   5. Compare & sync routes (add missing, remove stale)                          │   │
│   └─────────────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                                    │
│                                    ▼                                                    │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐   │
│   │                      VPC SUBNET ROUTE TABLES (Any Region)                       │   │
│   │                        Tagged: TGWRouteSync=enabled                             │   │
│   └─────────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### Event-Driven Mode
1. A route change occurs in the TGW route table (VPN learns new route, static route added, etc.)
2. Network Manager detects the change and emits an event to EventBridge in `us-west-2`
3. EventBridge rule matches the event and triggers the Lambda function
4. Lambda extracts TGW info from the event and syncs routes to tagged VPC route tables

### Scheduled Mode (Backup)
1. CloudWatch scheduled rule triggers Lambda periodically
2. Lambda processes ALL configured TGWs from `TGW_CONFIG`
3. Ensures routes stay in sync even if events are missed

### Route Sync Logic
```
TGW Routes (Source of Truth)     VPC Route Table (Target)
─────────────────────────────    ─────────────────────────
10.0.0.0/16          ═══════════ 10.0.0.0/16        (match)
192.168.1.0/24       ═══════════ 192.168.1.0/24     (match)
192.168.3.0/24       ──────────► (missing)          TO ADD
(missing)            ◄────────── 172.31.0.0/16      TO REMOVE
```

---

## Prerequisites

- [ ] AWS Account with appropriate permissions
- [ ] Transit Gateway with VPC attachments
- [ ] Transit Gateway registered with AWS Network Manager
- [ ] Global Network created in Network Manager

---

## Regional Requirements

> **CRITICAL**: Lambda and EventBridge **MUST** be deployed in `us-west-2` (PDX)

| Component | Region | Reason |
|-----------|--------|--------|
| **Lambda Function** | `us-west-2` | Required to receive Network Manager events |
| **EventBridge Rule** | `us-west-2` | Network Manager emits events only to this region |
| **IAM Role** | Global | IAM is a global service |
| **Transit Gateway** | Any region | Lambda makes cross-region API calls |
| **VPCs & Route Tables** | Any region | Lambda makes cross-region API calls |
| **Network Manager** | Global | Global service, no specific region |

### Why us-west-2?

AWS Network Manager emits **all** routing events to EventBridge exclusively in `us-west-2`, regardless of where your Transit Gateways are located. This is an AWS architectural decision for Network Manager's global event aggregation.

---

## Setup Guide

### Step 1: Register TGW with Network Manager

> Skip if your TGW is already registered with Network Manager.

#### 1.1 Create Global Network

**Console:**
1. Go to **VPC** → **Network Manager** → **Global networks**
2. Click **Create global network**
3. Enter a name and description
4. Click **Create**

#### 1.2 Register Transit Gateway

**Console:**
1. Go to **Network Manager** → **Transit gateway registrations**
2. Click **Register transit gateway**
3. Select your Transit Gateway
4. Click **Register**

#### 1.3 Verify Registration

Wait for status to become `AVAILABLE`

---

### Step 2: Create IAM Role

#### 2.1 Create Trust Policy

**Console:**
1. Go to **IAM** → **Roles** → **Create role**
2. Select **AWS service** → **Lambda**
3. Click **Next**


#### 2.2 Attach Permissions Policy

See [IAM Policy Reference](#-iam-policy-reference) for the complete policy.

---

### Step 3: Create Lambda Function

> **Important**: Create Lambda in `us-west-2` region

**Console:**
1. Switch to **us-west-2** region
2. Go to **Lambda** → **Create function**
3. Configure:

| Setting | Value |
|---------|-------|
| Function name | `TGWRouteSyncFunction` |
| Runtime | Python 3.12 |
| Architecture | x86_64 |
| Execution role | Use existing role → `TGWRouteSyncLambdaRole` |

4. Click **Create function**
5. Paste the [Lambda code](#-lambda-code)
6. Click **Deploy**
7. Go to **Configuration** → **General configuration** → **Edit**:

| Setting | Value |
|---------|-------|
| Timeout | 1 min |
| Memory | 256 MB |

8. Go to **Configuration** → **Environment variables** → **Edit**:

| Key | Value |
|-----|-------|
| `TGW_CONFIG` | See [Configuration](#-configuration) |

---

### Step 4: Create EventBridge Rule

> **Important**: Create EventBridge rule in `us-west-2` region

**Console:**
1. Switch to **us-west-2** region
2. Go to **EventBridge** → **Rules** → **Create rule**
3. Configure:

| Setting | Value |
|---------|-------|
| Name | `TGWRouteChangeRule` |
| Event bus | default |
| Rule type | Rule with an event pattern |

4. Click **Next**
5. Select **Custom patterns (JSON editor)**
6. Paste:

```json
{
    "source": ["aws.networkmanager"],
    "detail-type": ["Network Manager Routing Update"]
}
```

7. Click **Next**
8. Configure target:

| Setting | Value |
|---------|-------|
| Target type | AWS service |
| Target | Lambda function |
| Function | TGWRouteSyncFunction |

9. Click **Next** → **Create rule**

---

### Step 5: Tag VPC Route Tables

Tag the route tables you want to sync:

**Console:**
1. Go to **VPC** → **Route tables**
2. Select the route table
3. Click **Tags** → **Manage tags**
4. Add tag:

| Key | Value |
|-----|-------|
| `TGWRouteSync` | `enabled` |

5. Click **Save**

---

## Configuration

### Environment Variable: TGW_CONFIG

The Lambda function uses a JSON configuration to support multiple TGWs across regions:

#### Single TGW

```json
[{"tgw_id": "tgw-XXXXXXXXXXXX", "region": "ap-southeast-2"}]
```

#### Multiple TGWs

```json
[
    {"tgw_id": "tgw-XXXXXXXXXXXX", "region": "ap-southeast-2"},
    {"tgw_id": "tgw-XXXXXXXXXXXX", "region": "us-east-1"},
    {"tgw_id": "tgw-XXXXXXXXXXXX", "region": "eu-west-1"}
]
```

> **Note**: In the Lambda console, enter the JSON as a single line without line breaks.

### Configuration Fields

| Field | Description | Example |
|-------|-------------|---------|
| `tgw_id` | Transit Gateway ID | `tgw-XXXXXXXXXXXX` |
| `region` | AWS region where TGW is deployed | `ap-southeast-2` |

---

## Testing

### Test Event (Console)

1. Go to **Lambda** → **TGWRouteSyncFunction**
2. Go to **Test** tab
3. Create new test event:

```json
{
    "version": "0",
    "id": "test-001",
    "detail-type": "Network Manager Routing Update",
    "source": "aws.networkmanager",
    "region": "us-west-2",
    "detail": {
        "changeType": "TGW-ROUTE-INSTALLED",
        "transitGatewayArn": "arn:aws:ec2:<REGION>:<ACCOUNT-ID>:transit-gateway/<TGW-ID>",
        "transitGatewayRouteTableArns": [
            "arn:aws:ec2:<REGION>:<ACCOUNT-ID>:transit-gateway-route-table/<TGW-RTB-ID>"
        ],
        "region": "<TGW-REGION>"
    }
}
```

4. Click **Test**

### Force Sync All TGWs

To trigger a full sync of all configured TGWs or to Sync on an existing configuration, invoke with an empty detail:

```json
{
    "source": "manual-trigger",
    "detail-type": "Force Sync",
    "detail": {}
}
```

---

## Troubleshooting

### Lambda Not Triggering

| Check | How to Verify |
|-------|---------------|
| Lambda region | Must be in `us-west-2` |
| EventBridge region | Must be in `us-west-2` |
| EventBridge rule enabled | Check rule status is `Enabled` |
| Event pattern | Verify pattern matches actual events |
| Lambda permission | Check resource-based policy allows EventBridge |

### No Routes Being Synced

| Check | How to Verify |
|-------|---------------|
| Route table tagged | Verify `TGWRouteSync=enabled` tag exists |
| TGW in config | Verify TGW ID is in `TGW_CONFIG` |
| VPC associated | Verify VPC attachment is associated with TGW route table |
| IAM permissions | Check Lambda role has required permissions |

### View Logs

```bash
aws logs tail /aws/lambda/TGWRouteSyncFunction --follow --region us-west-2
```

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `TGW not found in TGW_CONFIG` | TGW ID missing from config | Add TGW to `TGW_CONFIG` environment variable |
| `UnauthorizedOperation` | Missing IAM permission | Update IAM policy |
| `No VPC attachments found` | VPC not associated with TGW route table | Check TGW route table associations |
| `No tagged route tables` | Missing tag | Add `TGWRouteSync=enabled` tag |

---

## IAM Policy Reference

[View IAM Policy](src/iam.json)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

---

## Warning

This repository contains experimental or development code.

**Do not use this code in production environments without verifying function on non-production workloads.**  

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

<p align="center">
  AWS TGWRouteSync 2026
</p>
