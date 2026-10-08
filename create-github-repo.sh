#!/bin/bash
# Script to create GitHub repo and push AKS Terraform infrastructure
# Run this script ON YOUR LOCAL MACHINE where GitHub CLI is installed

set -e

REPO_NAME="aks"
OWNER="kovokilla"

echo "=== Creating GitHub Repository ==="
echo "Repository: $OWNER/$REPO_NAME"

# Check if we're already in the right directory
if [ ! -d "terraform" ]; then
    echo "Changing to azure-aks-demo directory..."
    cd /Users/matus/azure-aks-demo || exit 1
fi

# Initialize git repo if not already
if [ ! -d ".git" ]; then
    echo "Initializing git..."
    git init
    git config user.email "you@example.com"
    git config user.name "Azure AKS Builder"
fi

# Add .gitignore if it exists or create one
cat > .gitignore << 'EOF'
# Terraform
*.tfstate
*.tfstate.old
*.tfplan
.terraform/
crash.log
.tfls.lock

# Node.js
node_modules/
package-lock.json
npm-debug.log

# Misc
.DS_Store
.env.local
*.local

# IDE
.vscode/
.idea/
*.swp
*.swo

# Python
__pycache__/
*.py[cod]
*$py.class
.python-version
venv/
env/
EOF

# Add all files
echo "Adding files to git..."
git add .

# Commit
echo "Committing changes..."
git commit -m "Initial commit: Azure AKS Terraform infrastructure

- Terraform configuration for AKS cluster deployment
- Kubernetes manifests (namespace, deployment, service, ingress)
- Sample Node.js application with Dockerfile
- Azure DevOps pipelines (build and deploy)
- Complete documentation in README.md"

# Try to create remote repo using gh CLI
echo "Creating remote repository..."
if command -v gh &> /dev/null; then
    # Create the repo
    gh repo create $OWNER/$REPO_NAME --public --source=. --remote=origin --push
    echo "✅ Repository created and pushed successfully!"
else
    echo "GitHub CLI (gh) not found. Creating remote manually..."
    
    # Add remote
    git remote add origin https://github.com/$OWNER/$REPO_NAME.git
    
    # Push to main branch
    git branch -M main
    git push -u origin main
    
    echo "✅ Files pushed to https://github.com/$OWNER/$REPO_NAME"
    echo "Note: You may need to create the repo manually first via GitHub UI"
fi

echo ""
echo "=== Next Steps ==="
echo "1. Configure Azure DevOps service connections:"
echo "   - Azure Resource Manager"
echo "   - Azure Kubernetes Service"
echo "   - Docker Registry"
echo ""
echo "2. Create variable group in Azure DevOps: aks-secrets"
echo "3. Run Azure DevOps build pipeline to deploy infrastructure"
echo "4. Update deployment.yaml with your GitHub container image name"