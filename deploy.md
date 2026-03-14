## Deployment: Oracle Cloud Always Free — Multi-MCP Host

This VM hosts multiple MCP servers behind a shared Caddy reverse proxy. The steam-mcp repo root serves as the host-level config (`docker-compose.yml`, `Caddyfile`). Each additional MCP lives in its own subdirectory.

### Server layout

```
~/mcps/                  ← git clone of this repo
  docker-compose.yml
  Caddyfile
  .env                   ← created manually on server (not in git)
  Dockerfile
  steam_mcp/
  data/
    steam/               ← steam.db lives here (persists across redeploys)
    other-mcp/           ← future MCP data volumes
  other-mcp/             ← future MCP source (git submodule or separate clone)
```

### Key resource decisions

- **Shape**: VM.Standard.A1.Flex — 1 OCPU, 6 GB RAM (within the 4 OCPU / 24 GB allowance)
- **Boot volume**: 50 GB (within the 200 GB total limit)
- **Networking**: public IP, 10 TB/month outbound
- **OS**: Ubuntu 22.04 or 24.04

> **Idle reclamation warning**: Oracle reclaims Always Free instances idle for 7 days (CPU, network, and memory all < 20th percentile). The MCP server's background HLTB pre-warm and periodic library syncs should keep it active.

---

### 1. Create Oracle Cloud account

1. Go to **cloud.oracle.com** → "Start for free"
2. Sign up — credit card required for identity verification; Always Free resources are never charged
3. Choose your **home region** during signup — **you cannot change it later**, Always Free compute is home-region only
4. Wait for activation email

---

### 2. Provision the VM

Use the Resource Manager stack (Terraform) to provision. If using the retry script:

```bash
chmod +x retry_deploy.sh
./retry_deploy.sh
```

The script retries until OCI capacity is available (A1 Flex is in high demand). It handles 429 rate limiting with a 10-minute backoff.

**Manual alternative**: Console → Compute → Instances → Create Instance
- Image: Canonical Ubuntu 22.04
- Shape: Ampere → VM.Standard.A1.Flex, 1 OCPU, 6 GB RAM
- SSH keys: upload `~/.ssh/id_ed25519.pub`
- Note the **Public IP** after creation

---

### 3. Open firewall ports

Oracle has two firewall layers — both must be opened.

**Security List (Oracle Console):**
1. Instance page → click your **Subnet** → Security Lists → default → Add Ingress Rules
2. Add:
   - Source CIDR: `0.0.0.0/0`, Protocol: TCP, Port: `80`
   - Source CIDR: `0.0.0.0/0`, Protocol: TCP, Port: `443`

**OS-level (after SSHing in):**
```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo apt install -y iptables-persistent
```

---

### 4. Add DNS records in Cloudflare

The VM's public IP is static for the lifetime of the instance — set A records once and forget them.

In the Cloudflare dashboard for your domain:
1. DNS → Add record
2. Type: **A**, Name: `steam`, Content: `<VM public IP>`, Proxy: **DNS only** (grey cloud — Caddy handles TLS)
3. Repeat for each future MCP subdomain (`notes`, etc.)

---

### 5. SSH into the instance

```bash
ssh ubuntu@<YOUR_PUBLIC_IP>
```

---

### 6. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
newgrp docker
```

---

### 7. Clone the repo

**If the repo is public:**
```bash
git clone https://github.com/yourusername/steam-mcp ~/mcps
```

**If the repo is private**, set up a deploy key first:
```bash
# On the server — generate a read-only deploy key
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""
cat ~/.ssh/deploy_key.pub
```
Copy the output, then in GitHub: repo → Settings → Deploy keys → Add deploy key (read-only). Then:
```bash
# Configure SSH to use the deploy key for GitHub
cat >> ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/deploy_key
EOF

git clone git@github.com:yourusername/steam-mcp ~/mcps
```

---

### 8. Configure the server

```bash
cd ~/mcps
mkdir -p data/steam
nano .env
```

```
DATABASE_URL=file:/data/steam.db
STEAM_API_KEY=your-key-from-steamcommunity.com/dev/apikey
STEAM_ID=your-64bit-steamid
MCP_AUTH_TOKEN=<generate below>
PORT=8000
STEAM_PROFILE_ID=your-steam-community-profile-id   # your steamcommunity.com/id/<this part>
BACKLOGGD_USER=your-backloggd-username             # your backloggd.com/u/<this part>
```

`docker-compose.yml` uses `env_file: .env`, so every variable in this file is automatically injected into the container — no other configuration needed.

Generate a token:
```bash
openssl rand -hex 32
```

Update the Caddyfile with your actual domain:
```bash
nano ~/mcps/Caddyfile
```
```
steam.yourdomain.com {
    reverse_proxy steam-mcp:8000
}
```

---

### 9. Upload the database (first deploy only)

```bash
# From local machine
scp path/to/steam-mcp/steam.db ubuntu@<IP>:~/mcps/data/steam/steam.db
```

---

### 10. Deploy

```bash
cd ~/mcps
docker compose up -d --build
docker compose logs -f
```

Expect `Library sync: {'games_upserted': 1981, ...}` within ~30 seconds.

---

### 11. Verify

```bash
curl https://steam.yourdomain.com/health
# {"status": "ok", "library_synced_at": "..."}
```

---

### 12. Configure Claude to use steam-mcp

In your Claude MCP config (`~/.claude/mcp_settings.json` or equivalent):
```json
{
  "mcpServers": {
    "steam": {
      "url": "https://steam.yourdomain.com/sse",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

---

### Redeploying after code changes

```bash
# From local machine — push changes
git push

# On server
cd ~/mcps && git pull && docker compose up -d --build steam-mcp
```

---

### Adding a new MCP

1. **Add an A record** in Cloudflare: `notes.yourdomain.com → <same VM IP>`

2. **Add the service** to `~/mcps/docker-compose.yml`:
   ```yaml
   notes-mcp:
     build: ./notes-mcp
     restart: always
     expose:
       - "8001"
     volumes:
       - ./data/notes:/data
     env_file: ./notes-mcp/.env
   ```

3. **Add a Caddy block** to `~/mcps/Caddyfile`:
   ```
   notes.yourdomain.com {
       reverse_proxy notes-mcp:8001
   }
   ```

4. **Add the new MCP source** to the repo (subdirectory with its own Dockerfile), commit and push. On the server:
   ```bash
   cd ~/mcps && git pull && docker compose up -d --build
   ```

---

### Resource usage

| Resource | Used | Always Free limit |
|---|---|---|
| A1 compute | 1 OCPU, 6 GB | 4 OCPU, 24 GB |
| Boot volume | 50 GB | 200 GB total |
| Outbound transfer | ~negligible | 10 TB/month |
| Public IP | 1 | 1 per instance |

6 GB RAM supports ~10–15 lightweight Python/Node MCPs at idle (~200–400 MB each).
