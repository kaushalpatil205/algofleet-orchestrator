# TradOps: Algorithmic Trading DevOps Infrastructure

Welcome to the **TradOps** project! This repository contains the complete, production-grade **DevOps infrastructure** built to orchestrate, scale, and monitor a high-frequency algorithmic trading engine on AWS.

> 🔒 **Security Notice:** The actual Python trading algorithms (`strategy-engine`) contain proprietary intellectual property and financial strategies. To prevent IP leakage, the trading engine is securely stored in a **separate, private GitHub repository**. 
>
> This public repository serves strictly as a portfolio demonstration of the **DevOps architecture, CI/CD pipelines, and infrastructure as code** used to deploy and manage those private algorithms securely.

## 🏗️ Architecture Overview

The infrastructure wraps around the private trading engine using a **Universal Container Pattern**. Every strategy runs in an isolated Kubernetes pod from the same base Docker image, injecting configurations dynamically at runtime via AWS Secrets Manager.

### Core Technologies
- **Infrastructure as Code:** Terraform (AWS VPC, EKS, RDS, ECR, Secrets Manager, ALB)
- **Containerization:** Docker (Multi-stage builds compiling `TA-Lib` C-libraries)
- **Orchestration:** Kubernetes (EKS, Vertical Pod Autoscalers, Liveness Probes)
- **CI/CD:** Jenkins (EKS-native declarative pipelines, GitOps)
- **Configuration Management:** Ansible (Bastion host hardening, Docker/Kubectl provisioning)
- **Observability:** Prometheus, Grafana, ELK Stack (Fluent-Bit DaemonSets)

## 🚀 The CI/CD Flow (How it works)

When a developer adds a new strategy configuration to the private repository, the following automated flow occurs:

1. **Jenkins** detects the change and triggers a build.
2. Jenkins securely clones this public DevOps repo AND the private trading engine repo using stored GitHub credentials.
3. A Python script (`scripts/gen_k8s_deployments.py`) dynamically generates Kubernetes deployment YAMLs based on a `variants.json` source-of-truth table.
4. Jenkins builds the Docker image and pushes it to AWS ECR.
5. Jenkins applies the Kubernetes manifests via `kubectl`.
6. EKS pulls the image, injects the live MT5 trading credentials securely from AWS Secrets Manager, and starts the trading pod.

## 📈 Proof of Execution
*(Screenshots of AWS EKS, Grafana Dashboards, and Jenkins Pipelines will be added here once live deployment is complete).*

