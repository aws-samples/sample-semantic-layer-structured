#!/bin/bash
set -e

# Script to sync frontend .env file with deployed stack values
# Usage: ./scripts/sync-frontend-env.sh

echo "🔄 Syncing frontend .env with deployed stack values..."

# Get the project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_ENV="$PROJECT_ROOT/frontend/.env"

# Check if frontend .env exists
if [ ! -f "$FRONTEND_ENV" ]; then
    echo "❌ Error: frontend/.env not found at $FRONTEND_ENV"
    exit 1
fi

echo "📋 Fetching values from AWS CloudFormation stacks..."

# Fetch Auth Stack outputs
echo "  - Fetching Cognito details from semantic-layer-auth..."
USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name semantic-layer-auth \
    --query "Stacks[0].Outputs[?OutputKey=='semanticlayerUserPoolId'].OutputValue" \
    --output text 2>/dev/null || echo "")

USER_POOL_CLIENT_ID=$(aws cloudformation describe-stacks \
    --stack-name semantic-layer-auth \
    --query "Stacks[0].Outputs[?OutputKey=='semanticlayerUserPoolClientId'].OutputValue" \
    --output text 2>/dev/null || echo "")

# For direct auth mode, domain is typically empty
USER_POOL_DOMAIN=""

# Fetch API URL from Lambda API Stack
echo "  - Fetching API URL from semantic-layer-lambda-api..."
API_URL=$(aws cloudformation describe-stacks \
    --stack-name semantic-layer-lambda-api \
    --query "Stacks[0].Outputs[?OutputKey=='RestApiUrl'].OutputValue" \
    --output text 2>/dev/null || echo "")

# Validate we got the values
if [ -z "$USER_POOL_ID" ] || [ -z "$USER_POOL_CLIENT_ID" ]; then
    echo "❌ Error: Could not fetch Cognito details from semantic-layer-auth stack"
    echo "   Make sure the stack is deployed and you have AWS credentials configured"
    exit 1
fi

if [ -z "$API_URL" ]; then
    echo "⚠️  Warning: Could not fetch API URL from semantic-layer-lambda-api stack"
    API_URL="http://localhost:3001/api"
fi

# Backup existing .env
BACKUP_FILE="$FRONTEND_ENV.backup-$(date +%Y%m%d-%H%M%S)"
cp "$FRONTEND_ENV" "$BACKUP_FILE"
echo "💾 Backed up existing .env to: $(basename $BACKUP_FILE)"

# Update .env file
echo "✏️  Updating frontend/.env..."

# Use sed to update values (works on both macOS and Linux)
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sed -i '' "s|^REACT_APP_API_URL=.*|REACT_APP_API_URL=$API_URL|" "$FRONTEND_ENV"
    sed -i '' "s|^REACT_APP_USER_POOL_ID=.*|REACT_APP_USER_POOL_ID=$USER_POOL_ID|" "$FRONTEND_ENV"
    sed -i '' "s|^REACT_APP_USER_POOL_CLIENT_ID=.*|REACT_APP_USER_POOL_CLIENT_ID=$USER_POOL_CLIENT_ID|" "$FRONTEND_ENV"
    sed -i '' "s|^REACT_APP_USER_POOL_DOMAIN=.*|REACT_APP_USER_POOL_DOMAIN=$USER_POOL_DOMAIN|" "$FRONTEND_ENV"
else
    # Linux
    sed -i "s|^REACT_APP_API_URL=.*|REACT_APP_API_URL=$API_URL|" "$FRONTEND_ENV"
    sed -i "s|^REACT_APP_USER_POOL_ID=.*|REACT_APP_USER_POOL_ID=$USER_POOL_ID|" "$FRONTEND_ENV"
    sed -i "s|^REACT_APP_USER_POOL_CLIENT_ID=.*|REACT_APP_USER_POOL_CLIENT_ID=$USER_POOL_CLIENT_ID|" "$FRONTEND_ENV"
    sed -i "s|^REACT_APP_USER_POOL_DOMAIN=.*|REACT_APP_USER_POOL_DOMAIN=$USER_POOL_DOMAIN|" "$FRONTEND_ENV"
fi

echo ""
echo "✅ Successfully updated frontend/.env with values from deployed stacks"
echo ""
echo "📊 Updated values:"
echo "   REACT_APP_API_URL=$API_URL"
echo "   REACT_APP_USER_POOL_ID=$USER_POOL_ID"
echo "   REACT_APP_USER_POOL_CLIENT_ID=$USER_POOL_CLIENT_ID"
echo "   REACT_APP_USER_POOL_DOMAIN=$USER_POOL_DOMAIN"
echo ""
echo "🚀 You can now run 'npm start' in the frontend directory for local development"
echo ""
echo "💡 Note: The .env file is for LOCAL DEVELOPMENT only."
echo "   Production deployments use values from CDK stacks, not this file."
