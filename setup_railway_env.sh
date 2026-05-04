#!/bin/bash
# RoyalPark — Azericard Railway env setup
# Run: bash setup_railway_env.sh
# Requires: railway CLI authenticated + linked to backend service

set -e

echo "=== Setting Azericard env vars on Railway ==="

# --- Non-sensitive vars ---
railway variables set \
  AZERICARD_GATEWAY_URL="https://testmpi.3dsecure.az/cgi-bin/cgi_link" \
  AZERICARD_API_URL="https://testmpi.3dsecure.az/cgi-bin/cgi_link" \
  AZERICARD_LANG="AZ" \
  AZERICARD_COUNTRY="AZ" \
  AZERICARD_MERCH_GMT="+4" \
  AZERICARD_TERMINAL_ID="17204537" \
  AZERICARD_TERMINAL_UTILITY="17204537" \
  AZERICARD_TERMINAL_MAINTENANCE="17204537" \
  AZERICARD_TERMINAL_ADVANCE="17204537" \
  AZERICARD_TERMINAL_WALLET="17204538" \
  AZERICARD_GPAY_ENVIRONMENT="TEST" \
  AZERICARD_GPAY_GATEWAY="azericardgpay"

echo "✓ Non-sensitive vars set"

# --- MPI Public Key (from test_config_integration.txt) ---
MPI_KEY="-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAr6qI1IxeWEPxsziZ0WI4\nAJw3qQr4jbZNBAhLNdfkogKdBpIAuCXra6enqeWDTa8WPrjkO5Gg0XgzshORVKbC\nVRLXkdveY5elHdcOzSNtth+JzCnmx9tovHEgzLsGJZNNmizf8EW6uezhHVxx0qE3\nZcf6xrzmexibJKeUF/+Nt2KdqkOUE1esEfW/ttKDb7w6dTWDS4ticRO83fJmWxxO\niiipsg4DPm3DkcIua0A4ARyTADCqJwoNcB+MjcX50XukSoECgDEeoSn7X7IZeJUt\nOS9AWlT/9Lf1dNl0+Ex4u+MU+Jd30qAkEGuJ+EHcaZ/NSD/nNlC0LSjcdTiGFUX7\nBwIDAQAB\n-----END PUBLIC KEY-----"

railway variables set AZERICARD_MPI_PUBLIC_KEY="$MPI_KEY"
echo "✓ MPI public key set"

# --- Merchant Private Key (TEST key from audit) ---
MERCHANT_PRIV="-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAydnI80c/hd72G7LxPwoh9VMfqWx3j8sXKI3lI9hrNflkRBgU\nP7V6T5T1Gc6/LZ1t2ESFLseHNYSEaX/E9u/hp/efrIOHIpmVt2vcvY8mmYwycRxG\noSH9onXogzpMYup0RL+QowXizqKgB/bJRo4a3NxnCppH01EIZFqP+4ebtw0hAwAM\n3LL89Hm6nS6K/OEBlA8m3yNfglXc6eWWZZA4ump5XjsdFkgrQTFvlH5fkReemSJo\ntOCnRUCfm+7t+032qCCN6ZMmHC+UVli4hRhLG01v+mhr5bXUUsqLz8uF5fXjc6q8\n8GGiOQQDAeR3jMNyk3mMp3bRf56gC33GyA8HvQIDAQABAoIBAF/pqW/YChotkNom\nlDW1Nd+hiOjzjnh4M1/k1N3Mh10VTQyCOJTxQdYw3KpPsE4XgUuDe5l33AqVFmoe\n+VOxNpOeuXO65+qL+jU2/qPgMqJBmPJgUjtcsG2TA1Hf0M4rw+Wq9SpRsK0pX0uJ\ne6iX+7G41QmXt8t0tL0iv0nw7Q/0R1eKYmqKTiCbihkaxeBQtF/SKkDgoX6sX+NH\nL920NkT6z6T5gA5WIQ9aQhMuz114DNlHr1+AKBu2VCKK9ekV7EaCFjaNZeq2s45D\nMrFgRDYA2s/QlDo/qS7novrPiNd33mcCBHj3zHg41+z1dj2fUBJkNiiPDl8r8hdx\nnqFvKa0CgYEA9uPa3VxVqWikh/SqnMM8+2/73ZyfSMSkHYiHLL739NjIQNBMZyCn\n3sGbgCSxSNbDwJ/AKqtOapVkr1dFacnB1PtOFFEllyxBnVbVFS3z7vMCsEkog/Ea\nOJH1wQz0TaoDsAMJbamA9JlGvLqZ66G2m/vy6IvOz4ltaw5vAe0OwU8CgYEA0Ux8\nCY4vkIk+++MH+rpjXNtCqUsbhur1W6ZlrbgFoAAsPatgssmixR3KOTaxa/UeUr8B\nHjBgbmPlgjgXi98Ly67sdnuVtmVCFbPendo8yXlD3IRLuAWsxQZMIevVPch5AS4F\n0//wcR6hj7k+NKevdk46Y8h8Z+E138lHLbnl6zMCgYA4KE3xSf5mIVpDXoCsVbB6\nVNeKagTFLY1S9mog4HNQKzspmve2AXSNs6YmOLJmqgsutmekjQCyN7cGNyifzneb\ndWomLusI/tUR791aCvDQalAzPwDLOj1HntOyjLrJK6HZGGe9nO+rM24moZ8/PLJn\nuqBfCuYIyO6tikPvwTc4+wKBgALCX2BA35+oL4xikdhcXLL8sQRKWTKOJm3u46hG\npMxXND4b5Ep3Hg47Nk9KyUwDD0NIAVvEh4DtEDmHQ8g0SJOG2tc1CeQ9sYFXvbeX\nCPYfAyYFGHp0mLKAQsCvuz/1RKMfWDRTS3gyTy714jwPeeC1Z0+pdPppnaw1mxqf\nOMjBAoGBAJH+Uo0FIq33EIQpGe6w/9gd64OaBzvrldXznOQFFr7xDNJK2UPZQc3W\nZqA2bSRYam1QSwIL4ETq3oTX0k7j5FTdTY8O5+jis0aKL1mJfHpIrrqRcYN6IDQN\nDEZ65YE/bEYWzlYedF4Fxo/zSr0MW9qzx1XrbSnYXzlQBQG3Cg75\n-----END RSA PRIVATE KEY-----"

MERCHANT_PUB="-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAydnI80c/hd72G7LxPwoh\n9VMfqWx3j8sXKI3lI9hrNflkRBgUP7V6T5T1Gc6/LZ1t2ESFLseHNYSEaX/E9u/h\np/efrIOHIpmVt2vcvY8mmYwycRxGoSH9onXogzpMYup0RL+QowXizqKgB/bJRo4a\n3NxnCppH01EIZFqP+4ebtw0hAwAM3LL89Hm6nS6K/OEBlA8m3yNfglXc6eWWZZA4\nump5XjsdFkgrQTFvlH5fkReemSJotOCnRUCfm+7t+032qCCN6ZMmHC+UVli4hRhL\nG01v+mhr5bXUUsqLz8uF5fXjc6q88GGiOQQDAeR3jMNyk3mMp3bRf56gC33GyA8H\nvQIDAQAB\n-----END PUBLIC KEY-----"

# Set same key pair for all terminal categories (test uses one key pair)
railway variables set \
  AZERICARD_PRIVATE_KEY="$MERCHANT_PRIV" \
  AZERICARD_PUBLIC_KEY="$MERCHANT_PUB" \
  AZERICARD_PRIVATE_KEY_UTILITY="$MERCHANT_PRIV" \
  AZERICARD_PUBLIC_KEY_UTILITY="$MERCHANT_PUB" \
  AZERICARD_PRIVATE_KEY_MAINTENANCE="$MERCHANT_PRIV" \
  AZERICARD_PUBLIC_KEY_MAINTENANCE="$MERCHANT_PUB" \
  AZERICARD_PRIVATE_KEY_ADVANCE="$MERCHANT_PRIV" \
  AZERICARD_PUBLIC_KEY_ADVANCE="$MERCHANT_PUB" \
  AZERICARD_PRIVATE_KEY_WALLET="$MERCHANT_PRIV" \
  AZERICARD_PUBLIC_KEY_WALLET="$MERCHANT_PUB"

echo "✓ Merchant key pairs set for all terminal categories"
echo ""
echo "=== Done. Railway will redeploy automatically. ==="
echo "Test at: https://backend-production-9052.up.railway.app/api/azericard/wallet-config"
