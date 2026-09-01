# Serverless ECS Orchestrator — Detailed Flowcharts

This document breaks down the ultra-lean, cost-optimized Serverless architecture into distinct operational workflows. It demonstrates how we achieve high availability and Zero-Trust networking for under $40/month.

---

## 1. CI/CD Pipeline (Push-Based Serverless Automation)
Unlike Kubernetes which uses a Pull-based GitOps controller (ArgoCD), this architecture uses a highly automated Push-based model native to AWS.

```mermaid
flowchart LR
    DEV["👨‍💻 Developer"]
    
    subgraph GITHUB["GitHub (algofleet-orchestrator)"]
        CONFIG["variants.json"]
        GHA["⚙️ GH Actions
(deploy-ecs.yml)"]
        SCRIPT["🐍 Python Script
(gen_ecs_tasks.py)"]
    end
    
    subgraph AWS["AWS Cloud"]
        ECR["📦 AWS ECR"]
        API["📡 AWS ECS API"]
        ECS["☁️ ECS Fargate Cluster"]
    end

    DEV -->|1. Edit Config & Push| CONFIG
    CONFIG -->|2. Triggers| GHA
    GHA -->|3. Runs| SCRIPT
    SCRIPT -->|4. Generates| JSON["ECS Task Definitions (.json)"]
    
    GHA -->|5. aws ecr build| ECR
    GHA -->|6. aws ecs register-task-definition| API
    GHA -->|7. aws ecs update-service| API
    
    API -->|8. Instantly Updates| ECS
```

---

## 2. Zero-Trust Ingress Flow (Cloudflare Tunnels)
To eliminate the $18/month AWS Application Load Balancer fee, we use a Cloudflare Tunnel sidecar. This exposes the internal dashboard securely to the internet without requiring public inbound ports.

```mermaid
flowchart TD
    USER["👤 End User / Admin"]
    CF["☁️ Cloudflare Global Edge
(DDoS Protection & SSL)"]
    
    subgraph AWS["AWS VPC (Public Subnet)"]
        subgraph TASK["ECS Fargate Task (algofleet-dashboard)"]
            direction LR
            CF_DAEMON["🛡️ cloudflared
(Sidecar Container)"]
            DASH["📊 FastAPI Dashboard
(Port 8000)"]
        end
    end
    
    USER -->|1. Visits dashboard.domain.com| CF
    CF_DAEMON -->|2. Establishes secure outbound tunnel| CF
    CF -->|3. Routes traffic through tunnel| CF_DAEMON
    CF_DAEMON -->|4. Proxies localhost:8000| DASH
```

---

## 3. Trade Execution & Stateful Storage Flow
Fargate containers are stateless. To run a database without paying for AWS RDS, we mount a serverless Elastic File System (EFS) directly into the PostgreSQL container.

```mermaid
flowchart LR
    subgraph AWS["AWS VPC (ap-south-1)"]
        direction TB
        
        SM["🔐 Secrets Manager
(MT5 Credentials)"]
        EFS["💾 AWS EFS
(Persistent Storage)"]
        
        subgraph ECS["ECS Fargate Cluster"]
            BOTS["🤖 Strategy Bots
(Fargate Spot)"]
            PG["🗃️ PostgreSQL Container"]
        end
    end
    
    MT5["💹 MT5 Bridge API (Broker)"]
    
    %% Storage Flow
    PG <-->|1. Reads/Writes Data| EFS
    
    %% Secrets Flow
    BOTS -.->|2. Fetches via IAM Task Role| SM
    
    %% Trade Flow
    BOTS <-->|3. Records Trades| PG
    BOTS -->|4. Outbound POST /place-trade| MT5
```
