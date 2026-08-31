# AlgoFleet Orchestrator Flowchart

This flowchart demonstrates the end-to-end flow of the project, separating the infrastructure architecture from the data and trade flow.

## 1. CI/CD & Deployment Flow (GitHub Actions & Jenkins)

```mermaid
flowchart LR
    DEV["👨‍💻 Developer"] -->|git push| REPO["📦 Public DevOps Repository"]
    
    PRIV_REPO["🔒 Private Strategy Engine Repo"]
    
    REPO -->|1. triggers (variants.json)| GHA["⚙️ GitHub Actions"]
    GHA -->|2. Gen Manifests and Commit| REPO
    
    REPO -->|3. Triggers| JENKINS["⚙️ Jenkins CI/CD"]
    PRIV_REPO -->|4. Securely Cloned| JENKINS
    
    JENKINS -->|5. Build and Push| ECR["📦 ECR Registry"]
    
    REPO -->|6. Polls Git| ARGO["🐙 ArgoCD GitOps Controller"]
    
    subgraph "AWS EKS Cluster"
        ARGO -->|Syncs Apps| BOTS["🤖 Strategy Pods"]
        ARGO -->|Syncs Addons| ADDONS["🔌 K8s Addons"]
    end
```

## 2. Secrets Management Flow

```mermaid
flowchart TD
    SM["🔐 AWS Secrets Manager"]
    
    subgraph "AWS EKS Cluster"
        ESO["🔑 External Secrets Operator"]
        K8S_SEC["📋 K8s Secret engine-config"]
        BOTS["🤖 Strategy Pods"]
    end
    
    ESO -->|Fetches via IRSA hourly| SM
    ESO -->|Creates or Updates| K8S_SEC
    K8S_SEC -->|Injected as ENV| BOTS
```

## 3. Trade Execution & Data Flow

```mermaid
flowchart TD
    subgraph "AWS EKS Cluster"
        BOTS["🤖 Strategy Pods Python Algorithms"]
        PG["🗃️ PostgreSQL Trade Database"]
        DASH["📊 Trade Dashboard FastAPI"]
        PROM["📈 Prometheus Metrics"]
        GRAF["📊 Grafana Dashboards"]
    end
    
    MT5["💹 MT5 Bridge API Broker Server"]
    
    BOTS -->|1. Fetch Market Data and Send Trade| MT5
    BOTS -->|2. Record Trade Result| PG
    DASH -->|3. Query Status| BOTS
    PROM -->|4. Scrape metrics| BOTS
    GRAF -->|5. Visualize| PROM
```

## 4. Complete System Architecture with Bastion Host

```mermaid
flowchart LR
    subgraph AWS["AWS Cloud"]
        subgraph VPC["VPC"]
            subgraph PUB["Public Subnets"]
                NLB1["⚖️ NLB: Dashboard"]
                NLB2["⚖️ NLB: Grafana"]
                BASTION["🖥️ Bastion Host configured by Ansible"]
            end
            
            subgraph PRIV["Private Subnets"]
                subgraph EKS["☸️ EKS Cluster"]
                    subgraph TRADING["NS: trading"]
                        BOTS["🤖 Strategy Pods"]
                        PG["🗃️ PostgreSQL"]
                        DASH["📊 Dashboard"]
                    end
                    
                    subgraph MON["NS: monitoring"]
                        PROM["📈 Prometheus"]
                        GRAF["📊 Grafana"]
                    end
                end
                NAT["🔀 NAT Gateway"]
            end
        end
        
        SM["🔐 Secrets Manager"]
        ECR["📦 ECR"]
    end
    
    MT5["💹 MT5 Bridge API"]
    ADMIN["👨‍🔧 System Admin"]
    
    NLB1 --> DASH
    NLB2 --> GRAF
    BOTS --> NAT --> MT5
    BOTS --> PG
    BOTS -.-> ECR
    TRADING -.-> SM
    
    ADMIN -->|SSH Secure Access| BASTION
    BASTION -->|kubectl / helm| EKS
```
