# Serverless ECS Architecture (V2)

This directory contains the Terraform and deployment scripts for the Fargate-based Serverless architecture.

## Cost Optimization Highlights:
* **No Kubernetes Tax:** Eliminates the $73/mo EKS Control Plane fee.
* **Fargate Spot:** Reduces compute costs by ~70%.
* **Cloudflare Tunnels:** Eliminates the $18/mo AWS Load Balancer fee by utilizing a `cloudflared` sidecar container for secure inbound routing.
* **EFS Storage:** Swaps EBS volumes for EFS to persist the PostgreSQL database in a stateless Fargate environment.
