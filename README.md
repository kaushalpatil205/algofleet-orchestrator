# 🚀 AlgoFleet Orchestrator

![AWS](https://img.shields.io/badge/AWS-%23FF9900.svg?style=for-the-badge&logo=amazon-aws&logoColor=white)
![Kubernetes](https://img.shields.io/badge/kubernetes-%23326ce5.svg?style=for-the-badge&logo=kubernetes&logoColor=white)
![Terraform](https://img.shields.io/badge/terraform-%235835CC.svg?style=for-the-badge&logo=terraform&logoColor=white)
![ArgoCD](https://img.shields.io/badge/Argo%20CD-1e0b3e?style=for-the-badge&logo=argo&logoColor=#d16044)
![GitHub Actions](https://img.shields.io/badge/github%20actions-%232671E5.svg?style=for-the-badge&logo=githubactions&logoColor=white)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![Postgres](https://img.shields.io/badge/postgres-%23316192.svg?style=for-the-badge&logo=postgresql&logoColor=white)
![Grafana](https://img.shields.io/badge/grafana-%23F46800.svg?style=for-the-badge&logo=grafana&logoColor=white)

> **Production-grade algorithmic trading platform** — live trading bots running as Kubernetes microservices on AWS EKS, placing real trades on MetaTrader 5 (MT5) with full GitOps automation, secrets management, observability, and zero manual deployments.

---

## 📋 Table of Contents

- [Executive Summary](#-executive-summary)
- [Architecture Diagram](#-architecture-diagram)
- [Architecture — In-Depth Explanation](#-architecture--in-depth-explanation)
  - [Layer 1: Infrastructure (Terraform + AWS)](#layer-1-infrastructure-terraform--aws)
  - [Layer 2: Container Runtime (EKS + ECR + Docker)](#layer-2-container-runtime-eks--ecr--docker)
  - [Layer 3: Cluster Addons](#layer-3-cluster-addons)
  - [Layer 4: Workloads (Trading Namespace)](#layer-4-workloads-trading-namespace)
  - [Layer 5: Observability (Monitoring Namespace)](#layer-5-observability-monitoring-namespace)
  - [Layer 6: GitOps and CI/CD](#layer-6-gitops-and-cicd)
- [End-to-End Data Flow — How a Trade Happens](#-end-to-end-data-flow--how-a-trade-happens)
- [Project Directory Structure](#-project-directory-structure)
- [Strategy Bots — Complete List](#-strategy-bots--complete-list)
- [Tech Stack Table](#-tech-stack-table)
- [Prerequisites](#-prerequisites)
- [Step-by-Step Setup Guide](#-step-by-step-setup-guide)
- [How to Deploy a New Strategy](#-how-to-deploy-a-new-strategy)
- [Accessing Services](#-accessing-services)
- [Secrets Management Deep Dive](#-secrets-management-deep-dive)
- [Teardown Guide](#-teardown-guide)

---

## 🎯 Executive Summary

AlgoFleet Orchestrator is a **fully automated, cloud-native algorithmic trading platform** built to demonstrate production-level DevOps engineering. It runs **independent trading strategy pods** as Kubernetes pods on AWS EKS. Each bot implements a different trading strategy (various proprietary trading strategies) across multiple financial instruments (various financial instruments).

Every bot connects to a **MetaTrader 5 bridge API** to place real trades, reads market signals using Python algorithms, records every trade in an internal **PostgreSQL database**, and is monitored via a **Grafana/Prometheus observability stack**.

The entire platform is managed using **GitOps principles** — the Git repository is the single source of truth. Push to `main` → GitHub Actions generates deployment manifests → ArgoCD syncs them to the cluster. No manual `kubectl apply` ever needed in production.

---

## 🏗️ Architecture Diagram

```mermaid
flowchart LR
    DEV["fa:fa-user Developer"] -->|"git push"| REPO["fa:fa-box Public Git Repository"]

    subgraph AWS["AWS Cloud (ap-south-1)"]
        direction TB
        SM["fa:fa-lock Secrets Manager"]
        ECR["fa:fa-box ECR Registry"]
        BASTION["fa:fa-server Bastion Host
(Configured via Ansible)"]

        subgraph VPC["VPC (10.0.0.0/16)"]
            direction TB
            subgraph PUB["Public Subnets"]
                NLB1["NLB: Dashboard"]
                NLB2["NLB: Grafana"]
            end

            subgraph PRIV["Private Subnets"]
                subgraph EKS["EKS Cluster (v1.34)"]
                    direction LR
                    subgraph NS_TRADING["NS: trading"]
                        BOTS["Strategy Pods"]
                        PG["PostgreSQL 15"]
                        DASH["Trade Dashboard"]
                    end

                    subgraph NS_MON["NS: monitoring"]
                        PROM["Prometheus"]
                        GRAF["Grafana"]
                    end

                    subgraph NS_SYS["NS: argocd & addons"]
                        ARGO["ArgoCD"]
                        ESO["External Secrets"]
                    end
                end
                NAT["NAT Gateway"]
            end
        end
    end

    MT5["MT5 Bridge API
Account: <YOUR_ACCOUNT_ID>"]
    PRIV_REPO["fa:fa-lock Private Strategy Engine Repo"]

    JENKINS["fa:fa-cog Jenkins CI/CD"]
    
    REPO -->|"pulls orchestration"| JENKINS
    PRIV_REPO -->|"pulls proprietary IP"| JENKINS
    JENKINS -->|"build/push"| ECR
    JENKINS -.->|"commit manifests"| REPO
    REPO -.->|"polls"| ARGO
    ARGO -->|"applies"| EKS
    
    BASTION -.->|"secure admin access"| EKS
    
    ESO -.->|"fetches"| SM
    BOTS -->|"reads/writes"| PG
    BOTS -->|"POST place-trade"| NAT
    NAT --> MT5
    
    NLB1 --> DASH
    NLB2 --> GRAF
    PROM -.->|"scrapes"| BOTS
    GRAF -.->|"queries"| PROM
```

---

## 🔍 Architecture — In-Depth Explanation

This platform is built in **6 distinct layers**, each with a clear responsibility. Here is an in-depth breakdown of every resource used, why it was chosen, and how it connects to the rest of the system.

---

### Layer 1: Infrastructure (Terraform + AWS)

All AWS infrastructure is defined as code in the `terraform/` directory. This means the entire platform can be created or destroyed with a single command, is version-controlled, and is perfectly reproducible.

#### S3 Backend — Terraform State Store

**Resource:** `S3 bucket: algofleet-tf-state-kaushal-2026`

**Why:** Terraform tracks what it has created in a "state file." By default this is stored locally, which makes team collaboration impossible. We store it in S3 so it is:
- **Remote** — accessible from any machine, including CI/CD runners
- **Encrypted** — `encrypt = true` ensures the state file is encrypted at rest
- **Locked** — `use_lockfile = true` prevents two simultaneous `terraform apply` runs from colliding and corrupting state

#### VPC — Virtual Private Cloud

**Resource:** `algofleet-vpc`, CIDR `10.0.0.0/16`, using the `terraform-aws-modules/vpc/aws` module

**Why:** EKS nodes must run inside a VPC for network isolation. We never expose worker nodes directly to the internet — they live in private subnets. A custom VPC gives us complete control over routing, security groups, and subnet design.

**Design decisions:**
- **2 Availability Zones** (`ap-south-1a`, `ap-south-1b`): Running in 2 AZs means if one data centre has an outage, the other continues serving traffic. Our EKS node group spans both, so Kubernetes automatically reschedules pods to healthy nodes.
- **Public Subnets** (`10.0.101.0/24`, `10.0.102.0/24`): These are where AWS Network Load Balancers are provisioned. NLBs need public IPs to accept internet traffic. Tagged with `kubernetes.io/role/elb = 1` so the AWS Load Balancer Controller knows these are the correct subnets for public LBs.
- **Private Subnets** (`10.0.1.0/24`, `10.0.2.0/24`): All EKS worker nodes run here. They have no public IP addresses, making them invisible to the internet. Tagged with `kubernetes.io/role/internal-elb = 1` for internal LBs, and `karpenter.sh/discovery = algofleet-eks` for Karpenter.
- **Single NAT Gateway**: Private-subnet nodes need outbound internet access to pull Docker images from ECR and call the MT5 bridge API. The NAT Gateway provides this one-way outbound access. We use a single NAT (rather than one per AZ) to save cost — acceptable for a portfolio environment. Production would use one NAT per AZ.

#### EKS Cluster — Elastic Kubernetes Service

**Resource:** `algofleet-eks`, Kubernetes v1.34, using `terraform-aws-modules/eks/aws`

**Why EKS over self-managed Kubernetes:** Running your own Kubernetes control plane (etcd, kube-apiserver, kube-scheduler, kube-controller-manager) is complex and operationally expensive. EKS manages the control plane entirely — AWS handles high availability, upgrades, backups, and security patching. We only manage the worker nodes.

**Configuration:**
- `cluster_endpoint_public_access = true`: The Kubernetes API server is accessible from the internet (secured by IAM + OIDC). This allows running `kubectl` from laptops and GitHub Actions runners without VPN.
- `enable_cluster_creator_admin_permissions = true`: Automatically grants the IAM identity that ran `terraform apply` full admin access via the EKS access entry system. No manual `aws-auth` ConfigMap editing needed.
- **Node Group** (`algofleet_nodes`): `t3.medium` instances (2 vCPU, 4 GB RAM), desired 4, min 2, max 6. Additional IAM policy `AmazonEBSCSIDriverPolicy` lets nodes attach EBS volumes for PostgreSQL.

#### EBS CSI Driver Addon

**Resource:** `aws-ebs-csi-driver`, installed as an EKS cluster addon

**Why:** PostgreSQL stores data on persistent disk. In Kubernetes, pods are ephemeral. The EBS CSI (Container Storage Interface) Driver is the bridge between Kubernetes' storage system and AWS EBS — it creates, attaches, and detaches EBS volumes automatically when pods request storage via PersistentVolumeClaims. Without it, the PostgreSQL StatefulSet cannot start.

#### Karpenter — Node Auto-Provisioner

**Resource:** Karpenter IAM roles + IRSA setup via Terraform

**Why Karpenter over Cluster Autoscaler:** The traditional Cluster Autoscaler can only scale fixed node groups with pre-defined instance types. Karpenter directly provisions EC2 instances of the optimal size for the workload. If a large pod needs 8 GB RAM, Karpenter picks an instance with 8 GB rather than waiting for a pre-defined group to scale. This saves cost and reduces scheduling latency.

#### ECR — Elastic Container Registry

**Resources:** Two repositories — `strategy-engine` and `trade-dashboard`

**Why ECR over Docker Hub:** ECR is AWS-native, meaning EKS nodes pull images using their existing IAM role — no external authentication needed. It is in the same region as the cluster, so pulls are fast (within the AWS backbone, no cross-internet charges). ECR also scans images for vulnerabilities on push (`scan_on_push = true`).

- `strategy-engine` — image for all strategy pods. One image, strategy pods — the specific strategy is selected by the `STRATEGY_SCRIPT` env variable.
- `trade-dashboard` — image for the FastAPI status dashboard.

#### AWS Secrets Manager

**Resource:** `aws_secretsmanager_secret` named `algofleet/engine-config`

**Why Secrets Manager over K8s Secrets or env variables:** Kubernetes Secrets are only base64-encoded (not truly encrypted) and live inside the cluster. AWS Secrets Manager provides encryption at rest via AWS KMS, fine-grained IAM access control, audit logging via CloudTrail, and versioning. No secret ever touches the Git repository.

**Secret contents (structure):**
```json
{
  "BOT_TOKEN": "<telegram-bot-token for trade alerts>",
  "CHAT_ID": "<telegram-chat-id>",
  "MT5_BRIDGE_URL": "<MT5_BRIDGE_URL>",
  "MT5_API_KEY": "<api-key>",
  "TRADE_DB_URL": "postgresql://algofleet:pass@postgres.trading.svc.cluster.local:5432/algofleet"
}
```

#### IRSA — IAM Roles for Service Accounts

**Resources:** `algofleet-external-secrets` and `algofleet-alb-controller` IAM roles

**Why IRSA:** The old approach was giving the EC2 node an IAM role, which means EVERY pod on that node inherits ALL permissions — a major security risk. IRSA binds IAM roles to specific Kubernetes Service Accounts using OIDC federation. Only pods using a specific Service Account can assume that specific IAM role. Zero trust, minimum privilege.

#### GitHub OIDC — Keyless CI/CD Authentication

**Resource:** `aws_iam_openid_connect_provider` + `aws_iam_role.github_actions`

**Why:** With GitHub OIDC, GitHub Actions generates a short-lived JWT token signed by GitHub. AWS trusts GitHub as an OIDC provider and exchanges this for a temporary AWS session (15 minutes). No long-lived credentials exist anywhere. The IAM role is scoped to only trust tokens from `repo:kaushalpatil205/*` — no other GitHub repo can assume this role.

---

### Layer 2: Container Runtime (EKS + ECR + Docker)

#### Docker — Container Images

**Why containers:** Each strategy bot is a Python application with dependencies (pandas, numpy, MT5 libraries, etc.). Containers package code AND dependencies together into a single immutable unit. This eliminates "works on my machine" problems.

**Single shared image design:** All strategy pods use the SAME `strategy-engine` Docker image. The `entrypoint.sh` reads the `STRATEGY_SCRIPT` environment variable to decide which Python file to run. This means pushing one new image updates all strategy pods on next rollout.

**How `ENGINE_CONFIG_JSON` flows into Python:** The Docker `entrypoint.sh` reads the `ENGINE_CONFIG_JSON` environment variable and writes its contents to `/app/Live/engine.json`. The Python code in `engine/config.py` then reads this file. Individual config values are also available as direct environment variables, which take absolute precedence.

---

### Layer 3: Cluster Addons

These are Kubernetes operators installed via ArgoCD Helm deployments that extend the cluster's native capabilities.

#### AWS Load Balancer Controller (v1.7.1)

**Namespace:** `kube-system`

**Why:** When you create a `LoadBalancer` Service in Kubernetes, the legacy in-tree cloud provider creates old **Classic Load Balancers (CLBs)**, which have limited features and often fail to properly route traffic in modern EKS setups (CLBs don't support the NLB-based target group binding that EKS security groups expect).

The AWS Load Balancer Controller creates modern **Network Load Balancers (NLBs)** when triggered by annotations on Services. NLBs operate at Layer 4 (TCP), have microsecond latency, and correctly integrate with AWS security groups.

**How it works:** The controller runs as a Deployment, watches for Services with annotation `service.beta.kubernetes.io/aws-load-balancer-type: nlb`, then calls the AWS API to create and configure the NLB. When the Service is deleted, it deletes the NLB automatically — preventing orphaned charges.

**IRSA:** Uses the `algofleet-alb-controller` IAM role (via IRSA on its Service Account) to make AWS API calls.

#### External Secrets Operator (v0.9.11)

**Namespace:** `external-secrets`

**Why:** Secrets must not live in Git. But pods need secrets at runtime. The External Secrets Operator watches for `ExternalSecret` custom resources, reaches out to AWS Secrets Manager, fetches the secret, and creates a standard Kubernetes Secret. Bot pods never touch AWS Secrets Manager directly.

**Components:**
- `ClusterSecretStore` (`aws-secrets`): Cluster-wide config pointing to AWS Secrets Manager in `ap-south-1`, authenticated via IRSA.
- `ExternalSecret` (`engine-config` in `trading` namespace): Says "fetch AWS secret `algofleet/engine-config`, create a K8s Secret named `engine-config` with key `json`. Refresh every 1 hour."
- The resulting K8s Secret is referenced in bot deployments via `secretKeyRef`.

#### gp3 StorageClass

**Why gp3 over gp2:** `gp3` is 20% cheaper than `gp2` and provides 3,000 IOPS as a baseline (gp2 baseline scales with volume size, making small volumes slower). Marked as default (`storageclass.kubernetes.io/is-default-class: "true"`) so PostgreSQL's PVC uses it automatically.

`volumeBindingMode: WaitForFirstConsumer` — the EBS volume is not created until a pod actually tries to mount it. This ensures the volume is created in the same Availability Zone as the pod (since EBS volumes are AZ-scoped).

---

### Layer 4: Workloads (Trading Namespace)

#### Strategy Bot Pods — The Core of AlgoFleet

**How each bot works:**
1. Pod starts, `entrypoint.sh` writes `ENGINE_CONFIG_JSON` environment variable to `/app/Live/engine.json`
2. The specific Python strategy file (e.g., `strategy.py`) executes
3. The strategy connects to the MT5 Bridge API using credentials from the injected secret
4. It fetches market data (OHLC candles) from MT5 via the bridge
5. It runs the strategy algorithm to generate buy/sell signals
6. On signal confirmation, it places a real trade via HTTP POST to the MT5 bridge
7. The trade result is recorded in PostgreSQL
8. A heartbeat file `/tmp/heartbeat` is updated periodically to prove the bot is alive

**Health Checks:**
- **Liveness Probe**: Checks `/tmp/heartbeat` was modified within the last 180 seconds. If the bot freezes, the heartbeat stops and Kubernetes automatically restarts the pod.
- **Readiness Probe**: Checks that `/tmp/heartbeat` exists. Until the bot has fully initialized, the pod is not marked Ready.

**Resource Limits:**
- `requests: memory: 256Mi, cpu: 100m` — minimum guaranteed resources for scheduling
- `limits: memory: 512Mi, cpu: 500m` — maximum allowed, preventing a misbehaving bot from crashing the node

#### PostgreSQL 15 — Trade Database

**Resource:** `StatefulSet` in `trading` namespace, `postgres:15-alpine` image

**Why StatefulSet over Deployment:** `Deployments` are for stateless pods. `StatefulSets` provide stable pod names (`postgres-0`), ordered startup, stable internal DNS (`postgres.trading.svc.cluster.local:5432`), and dedicated PersistentVolumeClaims — all critical for a database.

**PGDATA Configuration:** We set `PGDATA=/var/lib/postgresql/data/pgdata` (a subdirectory). Why? The EBS volume mounts at `/var/lib/postgresql/data`. When Kubernetes mounts an EBS volume, the mount root already has filesystem metadata. PostgreSQL refuses to initialize in a non-empty directory. Using a subdirectory gives PostgreSQL a clean, empty directory to initialize in.

**Persistent Volume:** 5 GiB EBS gp3 volume. Even if `postgres-0` is deleted and recreated, the same EBS volume is reattached and all trade data persists.

#### Trade Dashboard

FastAPI application exposed via an internet-facing NLB on port 80 → 8000. Shows which of the strategy pods are alive and their trade registration status.

---

### Layer 5: Observability (Monitoring Namespace)

#### Prometheus — Metrics Collection

**Resource:** Part of `kube-prometheus-stack` Helm chart v56.6.2

**Why Prometheus:** Works on a **pull model** — Prometheus scrapes HTTP `/metrics` endpoints on pods. No metrics are lost if Prometheus is temporarily down. Includes `kube-state-metrics` (Kubernetes object state) and `node-exporter` (EC2 host metrics). Uses a built-in Time Series Database (TSDB) for efficient metric storage.

**Why `kube-prometheus-stack`:** Bundles Prometheus + Grafana + Alertmanager + all CRDs (PrometheusRule, ServiceMonitor) in one Helm chart. `ServiceMonitor` CRDs let us declaratively configure what Prometheus scrapes — no manual config file editing.

#### Grafana — Visualization

Connects to Prometheus as a data source and renders metrics into interactive dashboards. Pre-built dashboards for Kubernetes cluster health (node CPU/memory, pod restarts) come included.

**Exposure:** Port 3000 inside cluster. Service is a `LoadBalancer` type with annotations:
- `service.beta.kubernetes.io/aws-load-balancer-type: nlb` — forces AWS LBC to create NLB instead of CLB
- `service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing` — places NLB in public subnets with a public IP

---

### Layer 6: GitOps and CI/CD

#### ArgoCD — GitOps Controller

**Namespace:** `argocd`

**Why GitOps:** Traditional deployments require manual `kubectl apply`. With GitOps, the Git repository is the single source of truth. ArgoCD continuously (every 3 minutes) compares desired state in Git with actual cluster state and automatically corrects differences.

This means:
- **Accidental manual changes are auto-reverted** (`selfHeal: true`)
- **Full audit trail** — every deployment is a git commit
- **Easy rollback** — `git revert` a commit and ArgoCD rolls back the cluster
- **Drift detection** — ArgoCD tells you if cluster differs from Git

**ArgoCD Applications:**
1. `algofleet-strategies` — all 13 bot Deployments + PostgreSQL StatefulSet + External Secrets resources
2. `prometheus-grafana` — full monitoring stack via Helm from prometheus-community repo
3. `aws-load-balancer-controller` — AWS LBC via Helm from eks-charts repo
4. `external-secrets` — ESO via Helm from external-secrets repo

#### Jenkins — CI/CD Pipeline & Private Strategy Integration

**Trigger:** Commit to the repository or scheduled builds.

**Why Jenkins over standard GitHub Actions:** The project separates the public orchestration infrastructure from the proprietary trading algorithms. Jenkins handles the secure integration of these two parts:
1. **Checkout Public DevOps Repo:** Pulls this infrastructure repository.
2. **Checkout Private Strategy Engine:** Securely pulls the proprietary trading code from the main private strategy repository (`strategy-engine`) using dedicated credentials. This ensures the trading IP remains completely hidden.
3. **Generate K8s Manifests:** Runs a Python script to dynamically generate Kubernetes deployment YAMLs for each strategy.
4. **Build & Push:** Packages the private strategy engine into a secure Docker image and pushes it to AWS ECR.
5. **Deploy:** Can either directly apply to EKS or let ArgoCD pick up the generated manifests for GitOps synchronization.

#### Ansible — Infrastructure Configuration

**Resource:** `ansible/playbooks/setup-bastion.yml`

**Why Ansible:** We use Ansible to configure a secure **Bastion Host (Jump Server)**. Instead of exposing the EKS cluster's API directly to the open internet for administration, the Bastion host acts as a secure gateway.
- **Automated Provisioning:** The Ansible playbook installs all necessary prerequisites on the Ubuntu machine (Docker, AWS CLI v2, kubectl, Helm).
- **Security:** It configures the UFW firewall to strictly allow only necessary ports (22, 80, 443).
- Administrators SSH into this Bastion host, and from there, securely interact with the private subnets of the EKS cluster.

---

## 🔄 End-to-End Data Flow — How a Trade Happens

```
1. MARKET TICK
   MT5 platform (broker server)
   → Bot connects via MT5 Bridge API (HTTP/REST)
   → Fetches OHLC candle data for the configured instrument (e.g., a specific timeframe)

2. SIGNAL GENERATION
   Python strategy algorithm (e.g., strategy.py)
   → Calculates indicators (EMA, RSI, momentum, etc.)
   → Detects setup conditions (pattern on a higher timeframe)
   → Waits for entry confirmation (pattern on a lower timeframe)

3. TRADE EXECUTION
   Bot sends HTTP POST to MT5 Bridge URL
   URL: <MT5_BRIDGE_URL>
   Payload: { symbol, action, volume, sl, tp, magic_number }
   → MT5 bridge forwards the order to the Exness MT5 server
   → Real trade is opened on the configured account

4. PERSISTENCE
   Bot records trade details (ticket, symbol, direction, entry price, SL, TP)
   → Writes to PostgreSQL at postgres.trading.svc.cluster.local:5432
   → Database: algofleet, Table: trades

5. TRAILING / MANAGEMENT
   Bot continues monitoring the open position
   → Updates trailing stop based on price movement
   → Closes position when TP/SL is hit or trailing stop triggers
   → Updates trade record in PostgreSQL (close price, P&L, duration)

6. MONITORING
   Prometheus scrapes bot metrics every 15s
   Grafana displays real-time dashboards
   Trade Dashboard shows bot registration and summary
```

---

## 📁 Project Directory Structure

```text
algofleet-orchestrator/
│
├── README.md                              ← This file
├── .github/
│   └── workflows/
│       └── render-manifests.yml           ← CI: auto-generates K8s YAMLs from variants.json
│
├── scripts/
│   ├── gen_k8s_deployments.py            ← Reads variants.json → writes deployment YAMLs
│   └── setup-secrets.sh                   ← Updates AWS Secrets Manager with MT5 credentials
│
├── variants/
│   └── variants.json                      ← Master config: all 13 strategy definitions
│
├── docker/
│   ├── strategy-engine/
│   │   ├── Dockerfile                     ← Base image for ALL strategy pods
│   │   └── entrypoint.sh                  ← Writes ENGINE_CONFIG_JSON to /app/Live/engine.json
│   └── trade-dashboard/
│       └── Dockerfile                     ← FastAPI dashboard image
│
├── strategy-engine/                       ← Git submodule: actual Python trading code
│   └── Live/
│       ├──                    ← proprietary strategies
│       ├──                    ← proprietary strategies
│       ├──                    ← proprietary strategies
│       ├── trade_db.py                    ← PostgreSQL integration layer
│       └── engine/
│           └── config.py                  ← Config resolution: Env > JSON > Default
│
├── kubernetes/
│   ├── namespaces.yaml                    ← Creates trading and monitoring namespaces
│   ├── argocd/
│   │   └── algofleet-app.yaml             ← ArgoCD Application for all strategies
│   ├── addons/
│   │   ├── aws-lbc.yaml                   ← ArgoCD App: AWS Load Balancer Controller
│   │   ├── external-secrets.yaml          ← ArgoCD App: External Secrets Operator
│   │   ├── prometheus-grafana.yaml        ← ArgoCD App: kube-prometheus-stack
│   │   └── storageclass.yaml              ← gp3 default StorageClass
│   ├── postgres/
│   │   └── statefulset.yaml               ← PostgreSQL StatefulSet + ClusterIP Service
│   └── strategies/
│       ├── aws-secret-store.yaml          ← ClusterSecretStore → AWS Secrets Manager
│       ├── engine-external-secret.yaml    ← ExternalSecret: algofleet/engine-config
│       ├── strategy-1/deployment.yaml
│       ├── strategy-2/deployment.yaml
│       ├── ... (11 more strategy directories, auto-generated)
│       └── trade-dashboard/
│           ├── deployment.yaml
│           └── service.yaml               ← NLB LoadBalancer Service
│
├── terraform/
│   ├── backend.tf                         ← S3 remote state backend
│   ├── main.tf                            ← All AWS resources (VPC, EKS, ECR, IAM, etc.)
│   └── outputs.tf                         ← Exported values (cluster name, ECR URL, role ARN)
│
└── docs/
    └── troubleshooting.md                 ← Complete troubleshooting guide with all issues
```

---

---

## 🛠️ Tech Stack Table

| Category | Technology | Version | Purpose |
|---|---|---|---|
| Cloud Provider | AWS | — | All infrastructure |
| Container Orchestration | Kubernetes (EKS) | 1.34 | Run and manage all pods |
| Infrastructure as Code | Terraform | — | Provision all AWS resources |
| GitOps | ArgoCD | v2.x | Sync Git state to Cluster |
| Configuration Management | Ansible | — | Setup Bastion Host |
| CI/CD Pipeline | Jenkins | — | Build images and integrate private strategy repo |
| Container Registry | Amazon ECR | — | Store Docker images |
| Load Balancer | AWS NLB | — | Expose services to internet |
| Secrets Management | AWS Secrets Manager | — | Store MT5 creds and API tokens |
| Secrets Bridge | External Secrets Operator | 0.9.11 | AWS Secrets Manager to K8s Secrets |
| Storage | AWS EBS gp3 | — | Persistent storage for PostgreSQL |
| Database | PostgreSQL | 15-alpine | Trade records persistence |
| Trading Execution | MT5 Bridge API | — | Place real MT5 trades |
| Monitoring | Prometheus | kps 56.6.2 | Metrics collection |
| Dashboards | Grafana | kps 56.6.2 | Metrics visualization |
| App Language | Python | 3.11 | Trading algorithms |
| Dashboard Backend | FastAPI | — | Trade status API |
| Node Autoscaling | Karpenter | — | Dynamic EC2 provisioning |
| EBS Provisioning | EBS CSI Driver | latest | Provision EBS PersistentVolumes |

---

## ✅ Prerequisites

```bash
aws --version        # AWS CLI v2
kubectl version      # kubectl 1.28+
terraform --version  # Terraform 1.6+
helm version         # Helm 3.x
argocd version       # ArgoCD CLI
docker --version     # Docker 24+
```

**AWS Requirements:**
- AWS Account in region `ap-south-1` (Mumbai) with Admin IAM permissions
- AWS SSO or IAM user configured via `aws configure`

---

## 🚀 Step-by-Step Setup Guide

### Step 1 — Bootstrap Terraform State Bucket

```bash
aws s3api create-bucket \
  --bucket algofleet-tf-state-kaushal-2026 \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

aws s3api put-bucket-versioning \
  --bucket algofleet-tf-state-kaushal-2026 \
  --versioning-configuration Status=Enabled
```

### Step 2 — Provision AWS Infrastructure via Terraform

```bash
cd terraform/
terraform init
terraform plan
terraform apply   # Takes ~15-20 minutes for EKS
```

### Step 3 — Configure kubectl

```bash
aws eks update-kubeconfig --name algofleet-eks --region ap-south-1
kubectl get nodes  # Verify 4 nodes are Ready
```

### Step 4 — Apply Prometheus CRDs (Must be done before ArgoCD syncs)

> The kube-prometheus-stack CRDs are too large for standard kubectl apply. Use server-side apply:

```bash
cd "/Users/Vraj/Downloads/Project D/algofleet-orchestrator"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm pull prometheus-community/kube-prometheus-stack --version 56.6.2 --untar
kubectl apply --server-side -f kube-prometheus-stack/charts/crds/crds/
rm -rf kube-prometheus-stack
```

### Step 5 — Install ArgoCD

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=120s

# Get admin password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

### Step 6 — Apply Cluster Addons

```bash
kubectl apply -f kubernetes/addons/storageclass.yaml
kubectl apply -f kubernetes/addons/aws-lbc.yaml
kubectl apply -f kubernetes/addons/external-secrets.yaml
kubectl apply -f kubernetes/addons/prometheus-grafana.yaml
```

### Step 7 — Upload Secrets to AWS Secrets Manager

```bash
# Edit scripts/setup-secrets.sh with your MT5 credentials first
bash scripts/setup-secrets.sh
```

### Step 8 — Deploy All Strategies

```bash
kubectl apply -f kubernetes/namespaces.yaml
kubectl apply -f kubernetes/argocd/algofleet-app.yaml
# ArgoCD syncs all strategy pods + PostgreSQL automatically
```

### Step 9 — Verify

```bash
kubectl get pods -n trading      # strategy pods + postgres-0 = Running
kubectl get pods -n monitoring   # prometheus + grafana = Running
kubectl get svc -n trading       # trade-dashboard has EXTERNAL-IP
kubectl get svc -n monitoring    # prometheus-grafana has EXTERNAL-IP
```

---

## ➕ How to Deploy a New Strategy

1. **Edit `variants/variants.json`** — add a new strategy entry with symbol, magic number, script path, risk.

2. **Commit and push:**
   ```bash
   git add variants/variants.json
   git commit -m "feat: add strategy 22 for a financial instrument"
   git push origin main
   ```

3. **GitHub Actions automatically** runs `gen_k8s_deployments.py`, generates `kubernetes/strategies/new-strategy/deployment.yaml`, commits and pushes back to `main`.

4. **ArgoCD automatically** (within 3 minutes) detects the new manifest and creates the new bot pod. It starts trading immediately.

---

## 🌐 Accessing Services

### Grafana Dashboard

```bash
kubectl get svc prometheus-grafana -n monitoring  # Get EXTERNAL-IP
# OR port-forward:
kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring
# Open: http://localhost:3000
```
**Username:** `admin` | **Password:** `admin`

### Trade Dashboard

```bash
kubectl get svc trade-dashboard -n trading  # Get EXTERNAL-IP
# Open the EXTERNAL-IP in browser on port 80
```

### Raw Prometheus UI

```bash
kubectl port-forward svc/prometheus-grafana-kube-pr-prometheus 9090:9090 -n monitoring
# Open: http://localhost:9090
```

### ArgoCD UI

```bash
kubectl port-forward svc/argocd-server 8080:443 -n argocd
# Open: https://localhost:8080
# Username: admin  |  Password: see Step 5
```

---

## 🔐 Secrets Management Deep Dive

```
AWS Secrets Manager: algofleet/engine-config
  └── Contains: MT5_BRIDGE_URL, MT5_API_KEY, BOT_TOKEN, CHAT_ID, TRADE_DB_URL
        │
        ▼ (External Secrets Operator polls every 1h via IRSA)
  Kubernetes Secret: engine-config (in trading namespace)
        └── key: json = <full secret as JSON string>
              │
              ▼ (mounted as environment variable by K8s)
  Bot Pod: ENGINE_CONFIG_JSON = <full JSON>
              │
              ▼ (entrypoint.sh writes to file)
  /app/Live/engine.json → read by Python engine/config.py
```

**To update a secret:**
```bash
bash scripts/setup-secrets.sh  # Update in AWS Secrets Manager
kubectl rollout restart deployment -n trading  # Force immediate pickup (ESO refreshes in 1h otherwise)
```

---

## 💣 Teardown Guide

> ⚠️ **Critical Order:** Delete ArgoCD apps FIRST. ArgoCD manages the LoadBalancer Services. When Services are deleted, the AWS Load Balancer Controller removes the actual NLBs from AWS. If you destroy EKS first, the NLBs become orphaned and keep charging you indefinitely.

```bash
# Step 1 — Delete ArgoCD apps (removes K8s resources including NLBs)
cd "/Users/Vraj/Downloads/Project D/algofleet-orchestrator"
kubectl delete application -n argocd algofleet-strategies prometheus-grafana

# Wait 2-3 minutes for NLBs to be deprovisioned by AWS Load Balancer Controller

# Step 2 — Destroy all AWS infrastructure via Terraform
cd terraform
terraform destroy -auto-approve

# Step 3 — Delete the S3 state bucket
aws s3 rm s3://algofleet-tf-state-kaushal-2026 --recursive
aws s3api delete-bucket --bucket algofleet-tf-state-kaushal-2026 --region ap-south-1

echo "All resources destroyed. No more charges."
```

---

## 📚 Further Reading

- [docs/troubleshooting.md](docs/troubleshooting.md) — Complete guide to all issues encountered and how they were resolved
- [AWS EKS Documentation](https://docs.aws.amazon.com/eks/)
- [ArgoCD Documentation](https://argo-cd.readthedocs.io/)
- [External Secrets Operator](https://external-secrets.io/)
- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/)
