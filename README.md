# AlgoFleet Orchestrator (Dual-Architecture Portfolio)

Welcome to the **AlgoFleet Orchestrator**. This repository contains the infrastructure and deployment automation for a fleet of algorithmic task microservices.

To demonstrate architectural maturity, cloud economics, and mastery of multiple DevOps toolchains, I have built and maintained **two distinct deployment targets** side-by-side within this single repository.

## 🏛️ The Architectures

### 1. [Enterprise GitOps Architecture (Kubernetes / EKS)](./enterprise-k8s-architecture)
This folder contains Version 1 of the project. It is designed for a large-scale, Fortune-500 enterprise environment that prioritizes strict GitOps workflows and Kubernetes-native toolsets.
* **Compute:** AWS Elastic Kubernetes Service (EKS)
* **CI/CD:** GitHub Actions (CI) + **ArgoCD** (CD)
* **Networking:** AWS Application Load Balancer via Ingress Controller
* **Storage:** EBS gp3 Persistent Volumes via CSI Driver
* **Cost:** ~$280 / month

### 2. [Serverless Ultra-Lean Architecture (ECS / Fargate)](./serverless-ecs-architecture)
This folder contains Version 2 of the project. It is designed for a lean startup environment, prioritizing **Cloud FinOps** and 100% serverless infrastructure, slashing the AWS bill by ~75%.
* **Compute:** AWS ECS with **Fargate Spot** (No Kubernetes Control Plane)
* **CI/CD:** Push-based Deployments via GitHub Actions natively to ECS API
* **Networking:** **Cloudflare Tunnels** (Zero-Trust edge routing, avoiding AWS ALB fees)
* **Storage:** AWS EFS (Elastic File System) for containerized Postgres
* **Cost:** ~$30 / month

## 📁 Shared Configuration
Both architectures share the same proprietary Python task processors (via Git Submodules) and the exact same configuration payload. 

* `variants/variants.json` — The master configuration file. When this file is edited, GitHub Actions utilizes path-based triggers to dynamically update **both** the Kubernetes YAMLs and the ECS Task Definitions simultaneously!

---
*Created to demonstrate advanced Cloud Infrastructure and DevOps Engineering skills.*
