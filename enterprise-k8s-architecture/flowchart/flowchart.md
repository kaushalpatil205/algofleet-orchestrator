# AlgoFleet Orchestrator — Detailed Flowcharts

This document breaks down the architecture into distinct operational workflows, demonstrating how the system handles Day-0 provisioning, two distinct CI/CD pipelines (GitOps vs Traditional), and real-time trade execution.

---

## 1. Day-0 Infrastructure & Security (Ansible)
Before any code is deployed, the environment must be secured. We never expose the Kubernetes control plane or worker nodes to the public internet.

```mermaid
flowchart TD
    ADMIN["👨‍🔧 System Administrator"]
    
    subgraph AWS["AWS Cloud (ap-south-1)"]
        subgraph PUB["Public Subnet"]
            BASTION["🖥️ Bastion Host\n(Ubuntu EC2)"]
        end
        
        subgraph PRIV["Private Subnet"]
            EKS["☸️ AWS EKS Cluster\n(Private Endpoint)"]
        end
    end
    
    ANSIBLE["⚙️ Ansible Playbook\n(setup-bastion.yml)"]
    
    ADMIN -->|Runs Playbook| ANSIBLE
    ANSIBLE -->|Installs Docker, AWS CLI, kubectl, Helm\nConfigures UFW Firewall| BASTION
    ADMIN -->|Secure SSH Tunnel| BASTION
    BASTION -->|Administers via kubectl| EKS
```

---

## 2. Pipeline Option A: Modern GitOps (GitHub Actions + ArgoCD)
This is the primary, highly-automated deployment path leveraging the dual-repository setup.

```mermaid
flowchart LR
    DEV["👨‍💻 Developer"]
    
    subgraph PRIV_REPO["🔒 Private Strategy Repo"]
        CODE["Proprietary Python Code"]
        GHA_PRIV["⚙️ GH Actions\n(4 Workflows)"]
    end
    
    subgraph PUB_REPO["📦 Public Orchestration Repo"]
        CONFIG["variants.json"]
        GHA_PUB["⚙️ GH Actions\n(render-manifests.yml)"]
    end
    
    ECR["📦 AWS ECR"]
    ARGO["🐙 ArgoCD\n(In EKS)"]
    EKS["☸️ EKS Cluster"]

    %% Flow Steps
    DEV -->|1. Push Code| CODE
    CODE -->|Triggers| GHA_PRIV
    GHA_PRIV -->|2. Run Syntax & Execution Tests| GHA_PRIV
    GHA_PRIV -->|3. Build & Push Image| ECR
    
    DEV -->|4. Push Config| CONFIG
    CONFIG -->|Triggers| GHA_PUB
    GHA_PUB -->|5. Generate K8s YAMLs| PUB_REPO
    
    PUB_REPO -->|6. Polls Every 3m| ARGO
    ARGO -->|7. Syncs Manifests| EKS
    EKS -.->|8. Pulls Image| ECR
```

---

## 3. Pipeline Option B: Traditional Enterprise (Jenkins)
As an alternative to GitOps, a centralized Jenkins server acts as a secure deployment bridge. This proves the ability to work in strict enterprise environments that avoid public GitHub Actions runners for proprietary IP.

```mermaid
flowchart TD
    DEV["👨‍💻 Developer"]
    
    PRIV_REPO["🔒 Private Strategy Repo"]
    PUB_REPO["📦 Public Orchestration Repo"]
    
    subgraph SECURE_ZONE["AWS Secure VPC"]
        JENKINS["⚙️ Jenkins Server\n(Jenkinsfile)"]
        ECR["📦 AWS ECR"]
        EKS["☸️ EKS Cluster"]
    end
    
    DEV -->|Pushes Updates| PRIV_REPO & PUB_REPO
    
    PUB_REPO -->|1. Clones Infrastructure| JENKINS
    PRIV_REPO -->|2. Clones IP via Secure Token| JENKINS
    
    JENKINS -->|3. Generate YAMLs & Build Image| JENKINS
    JENKINS -->|4. Push Image| ECR
    JENKINS -->|5. kubectl apply| EKS
```

---

## 4. Trade Execution & Observability Flow
Once the pods are deployed via either pipeline, this is how they interact with the broker and the monitoring stack.

```mermaid
flowchart LR
    subgraph EKS["AWS EKS Cluster"]
        direction TB
        
        ESO["🔑 External Secrets Operator"]
        K8S_SEC["📋 K8s Secret\n(engine-config)"]
        
        BOTS["🤖 Strategy Pods"]
        PG["🗃️ PostgreSQL Database"]
        
        PROM["📈 Prometheus"]
        GRAF["📊 Grafana"]
    end
    
    SM["🔐 AWS Secrets Manager"]
    NAT["🔀 NAT Gateway"]
    MT5["💹 MT5 Bridge API (Broker)"]
    
    %% Secrets Flow
    ESO -->|1. Fetches via IRSA| SM
    ESO -->|2. Updates| K8S_SEC
    K8S_SEC -->|3. Injected as ENV| BOTS
    
    %% Trade Flow
    BOTS -->|4. Process Data & Generate Signals| BOTS
    BOTS -->|5. POST /place-trade| NAT
    NAT --> MT5
    BOTS -->|6. Record Trade Result| PG
    
    %% Observability Flow
    PROM -->|7. Scrape /metrics| BOTS
    GRAF -->|8. Visualize Data| PROM
```
