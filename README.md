# TradOps: Distributed High-Frequency Processing Infrastructure

Welcome to the **TradOps** project! This repository contains the complete, production-grade **DevOps infrastructure** built to orchestrate, scale, and monitor a high-frequency, event-driven financial data processing engine on AWS.

> 🔒 **Security Notice:** The actual Python data processing algorithms (`strategy-engine`) contain proprietary intellectual property and statistical models. To prevent IP leakage, the core execution engine is securely stored in a **separate, private GitHub repository**. 
>
> This public repository serves strictly as a portfolio demonstration of the **Platform Engineering & DevOps architecture, CI/CD pipelines, and infrastructure as code** used to deploy and manage those private, low-latency applications securely.

## 🏗️ Architecture Overview

The infrastructure wraps around the private processing engine using a **Universal Container Pattern**. Every worker runs in an isolated Kubernetes pod from the same base Docker image, injecting configurations dynamically at runtime via AWS Secrets Manager. This abstracts the application logic away from the infrastructure layer, allowing seamless CI/CD.

### Core Technologies
- **Infrastructure as Code:** Terraform (AWS VPC, EKS, RDS, ECR, Secrets Manager, ALB)
- **Containerization:** Docker (Multi-stage builds compiling complex C-libraries)
- **Orchestration:** Kubernetes (EKS, Vertical Pod Autoscalers, Liveness Probes)
- **CI/CD:** Jenkins (EKS-native declarative pipelines, GitOps)
- **Configuration Management:** Ansible (Bastion host hardening, Docker/Kubectl provisioning)
- **Observability:** Prometheus, Grafana, ELK Stack (Fluent-Bit DaemonSets)

## 🚀 The CI/CD Flow (How it works)

When a developer adds a new processor configuration to the private repository, the following automated flow occurs:

1. **Jenkins** detects the change and triggers a build.
2. Jenkins securely clones this public DevOps repo AND the private execution engine repo using stored credentials.
3. A Python script (`scripts/gen_k8s_deployments.py`) dynamically generates Kubernetes deployment YAMLs based on a `variants.json` source-of-truth table.
4. Jenkins builds the Docker image and pushes it to AWS ECR.
5. Jenkins applies the Kubernetes manifests via `kubectl`.
6. EKS pulls the image, injects the live production execution credentials securely from AWS Secrets Manager, and starts the stateful worker pod.

## 📈 Proof of Execution
*(Screenshots of AWS EKS, Grafana Dashboards, and Jenkins Pipelines will be added here once live deployment is complete).*

