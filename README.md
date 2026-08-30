# 🚀 AlgoFleet Orchestrator

![Kubernetes](https://img.shields.io/badge/kubernetes-%23326ce5.svg?style=for-the-badge&logo=kubernetes&logoColor=white) ![AWS](https://img.shields.io/badge/AWS-%23FF9900.svg?style=for-the-badge&logo=amazon-aws&logoColor=white) ![Terraform](https://img.shields.io/badge/terraform-%235835CC.svg?style=for-the-badge&logo=terraform&logoColor=white) ![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)

## Executive Summary

AlgoFleet Orchestrator is a production-grade algorithmic trading platform running on AWS EKS. It orchestrates 13 trading bots as Kubernetes microservices, placing live trades on MetaTrader 5 (MT5) via an MT5 bridge URL. This project showcases advanced DevOps practices including Infrastructure as Code (Terraform), GitOps (ArgoCD), secret management (External Secrets Operator), and automated CI/CD pipelines (GitHub Actions).

## Architecture

```mermaid
graph TD
    subgraph AWS Cloud
        subgraph EKS Cluster
            subgraph Namespace: trading
                Bots[13 Strategy Bots]
                PG[(PostgreSQL)]
                ES[External Secret]
                TD[Trade Dashboard]
            end
            
            subgraph Namespace: monitoring
                Grafana[Grafana]
                Prom[Prometheus]
            end
            
            subgraph Namespace: argocd
                Argo[ArgoCD]
            end
            
            subgraph Addons
                LBC[AWS Load Balancer Controller]
                ESO[External Secrets Operator]
            end
        end
        
        SM[AWS Secrets Manager]
        ECR[Amazon ECR]
        S3[S3 Terraform Backend]
        NLB1[NLB - Dashboard]
        NLB2[NLB - Grafana]
    end
    
    GH[GitHub Actions]
    MT5[MT5 Bridge API]
    
    GH -->|Pushes Images| ECR
    GH -->|Updates Git| Argo
    Argo -->|Syncs YAML| EKS Cluster
    ESO -->|Syncs| SM
    ES -.->|Populates| Bots
    Bots -->|Reads/Writes| PG
    Bots -->|Sends Trades| MT5
    TD -.->|Query Status| Bots
    NLB1 --> TD
    NLB2 --> Grafana
```

## Table of Contents

- [Executive Summary](#executive-summary)
- [Architecture](#architecture)
- [Major Components](#major-components)
- [End-to-End Data Flow](#end-to-end-data-flow)
- [Prerequisites](#prerequisites)
- [Setup Guide](#setup-guide)
- [Deploying a New Strategy](#deploying-a-new-strategy)
- [Accessing Services](#accessing-services)
- [Secrets Management](#secrets-management)
- [GitOps with ArgoCD](#gitops-with-argocd)
- [Project Directory Structure](#project-directory-structure)
- [Teardown](#teardown)
- [Tech Stack](#tech-stack)

## Major Components

### Infrastructure (Terraform)
- **State Management**: S3 backend (`algofleet-tf-state-kaushal-2026`) with state locking.
- **Networking**: Custom VPC (`algofleet-vpc`) across 2 AZs, public/private subnets, and a single NAT Gateway.
- **Compute**: EKS v1.34 with 4x `t3.medium` nodes managed by Karpenter.

### Kubernetes Addons
- **AWS Load Balancer Controller**: Provisions AWS Network Load Balancers (NLBs).
- **External Secrets Operator**: Syncs secrets from AWS Secrets Manager to K8s secrets.
- **Kube-Prometheus-Stack**: Monitoring and Grafana dashboards.

### Workloads
- **Strategy Bots**: 13 unique trading algorithms deployed as microservices.
- **Postgres**: Internal database for trade history.
- **Trade Dashboard**: FastAPI application exposing current fleet status.

## End-to-End Data Flow
1. **Market Data**: Bots receive signals or tick data internally.
2. **Signal Generation**: Python algorithms process data and generate buy/sell signals.
3. **Execution**: Bot fires a request to the MT5 Bridge URL with the payload.
4. **Persistence**: The trade execution result is saved to the internal PostgreSQL database.
5. **Monitoring**: Trade Dashboard queries running pods and database to display active bots and overall system health.

## Prerequisites
- AWS Account with appropriate IAM permissions
- `aws` CLI, `kubectl`, `terraform`, `helm`, `argocd` CLI
- Docker

## Setup Guide
1. **Infrastructure**: Navigate to `terraform/` and run `terraform init`, `terraform apply`.
2. **Secrets**: Ensure `algofleet/engine-config` exists in AWS Secrets Manager.
3. **ArgoCD**: Install ArgoCD in the cluster and apply the `algofleet-strategies` Application manifest.
4. **Monitoring**: Apply the `kube-prometheus-stack` Helm chart.

## Deploying a New Strategy
1. Add the new strategy configuration to `variants/variants.json`.
2. Commit and push to `main`.
3. GitHub Actions triggers `scripts/gen_k8s_deployments.py`, auto-generates K8s deployment YAMLs, and commits them.
4. ArgoCD detects the Git change and auto-syncs the new bot to the EKS cluster.

## Accessing Services
- **Grafana**: Available via its internet-facing NLB on port 80 (mapped to 3000). Use admin credentials deployed via Helm values.
- **Trade Dashboard**: Available via its internet-facing NLB on port 80 (mapped to 8000).

## Secrets Management
We use the **External Secrets Operator**. A `ClusterSecretStore` points to AWS Secrets Manager. An `ExternalSecret` resource in the `trading` namespace pulls `algofleet/engine-config` and creates a Kubernetes secret with the MT5 credentials and DB URL. Bots mount this as an environment variable `ENGINE_CONFIG_JSON`.

## GitOps with ArgoCD
ArgoCD continuously monitors the GitHub repository. When the CI pipeline updates the deployment manifests, ArgoCD synchronizes the cluster state with the Git state, ensuring a single source of truth.

## Project Directory Structure
```text
.
├── .github/workflows/
├── scripts/
│   └── gen_k8s_deployments.py
├── variants/
│   └── variants.json
├── kubernetes/
│   ├── trading/
│   ├── monitoring/
│   └── argocd/
├── terraform/
└── docs/
```

## Teardown
To destroy all resources:
```bash
terraform destroy -auto-approve
```
*Note: Ensure you delete ArgoCD applications and Load Balancers manually if not managed purely by Terraform.*

## Tech Stack

| Category | Technology |
|---|---|
| Cloud | AWS (EKS, ECR, VPC, Secrets Manager) |
| Container Orchestration | Kubernetes |
| IaC | Terraform |
| GitOps | ArgoCD |
| CI/CD | GitHub Actions |
| Data | PostgreSQL |
| App Backend | Python, FastAPI |
| Observability | Prometheus, Grafana |

## AWS Services

| Service | Usage |
|---|---|
| EKS | Kubernetes cluster running the workloads |
| ECR | Docker image registry for bots and dashboard |
| VPC | Network isolation |
| Secrets Manager | Secure storage of MT5 credentials |
| S3 | Terraform state backend |
| NLB | Load balancing for incoming traffic |
| EBS | Persistent storage for PostgreSQL |
