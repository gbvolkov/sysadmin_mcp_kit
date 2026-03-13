# SSH Access Setup Summary

## Executive Summary

SSH access from the client machine to the Ubuntu server was verified and completed successfully.

The server was already listening on port 22, `ufw` was inactive, and the reachable LAN IP was identified as `172.16.1.31`. Initial SSH attempts reached the server but failed during authentication. After confirming SSH server settings and checking the user account state, login was ultimately confirmed to work. SSH key-based authentication was then configured using the existing client key `id_ed25519`, and key login succeeded.

## Environment

### Server
- Host/user: `volkov@cheetan`
- OS family: Ubuntu/Linux
- SSH service: OpenBSD Secure Shell server
- Relevant server LAN IP: `172.16.1.31`

### Client
- Windows PowerShell
- Existing SSH key material present in `C:\Users\volko\.ssh`

## 1. Initial Goal

Configure SSH so the client machine can access the server.

## 2. Server-Side SSH Verification

### SSH service status
Command run on server:

```bash
sudo systemctl status ssh --no-pager -l
```

Observed result:
- `ssh.service - OpenBSD Secure Shell server`
- `Loaded: loaded`
- `Active: inactive (dead)`
- `TriggeredBy: ssh.socket`

Interpretation:
- SSH server was installed.
- The service was socket-activated, so `ssh.service` showing inactive was not itself a fault condition.

### Port 22 listening check
Command run on server:

```bash
sudo ss -ltnp | grep ':22'
```

Observed result:

```text
LISTEN 0      4096         0.0.0.0:22         0.0.0.0:*    users:(("systemd",pid=1,fd=277))
LISTEN 0      4096            [::]:22            [::]:*    users:(("systemd",pid=1,fd=278))
```

Interpretation:
- SSH was listening on all IPv4 and IPv6 interfaces on port 22.

### Firewall check
Command run on server:

```bash
sudo ufw status verbose
```

Observed result:

```text
Status: inactive
```

Interpretation:
- `ufw` was not blocking SSH.

## 3. Server IP Identification

Command run on server:

```bash
hostname -I
```

Observed result:

```text
172.16.1.31 172.23.0.1 172.18.0.1 172.20.0.1 172.21.0.1 172.22.0.1 172.19.0.1 172.17.0.1
```

Interpretation:
- The relevant LAN address was identified as `172.16.1.31`.
- The other `172.17.x.x` through `172.23.x.x` addresses were Docker bridge/network addresses.

## 4. Client Connectivity Test

Command run on client:

```powershell
ssh volkov@172.16.1.31
```

Observed behavior:
- Host authenticity prompt appeared.
- The user accepted the server fingerprint.
- Connection reached the server.
- Password authentication initially failed with:

```text
Permission denied, please try again.
volkov@172.16.1.31: Permission denied (publickey,password).
```

Interpretation:
- Network connectivity was working.
- The issue at that point was authentication, not routing, firewall, or SSH reachability.

## 5. Authentication Checks

### Effective SSH authentication settings
Command run on server:

```bash
sudo sshd -T | egrep 'passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|usepam|authenticationmethods'
```

Observed result:

```text
usepam yes
pubkeyauthentication yes
passwordauthentication yes
kbdinteractiveauthentication no
authenticationmethods any
```

Interpretation:
- Password authentication was enabled.
- Public key authentication was enabled.
- PAM was enabled.

### User password/account state
Command run on server:

```bash
sudo passwd -S volkov
```

Observed result:

```text
volkov P 2024-11-03 0 99999 7 -1
```

Interpretation:
- The account password state was valid (`P`).

### Resolution of the password login question
The user later confirmed:
- password login from the client to the server succeeded after retrying correctly.

This established that SSH password-based access was already functional.

## 6. SSH Key-Based Authentication Setup

### Prepare `.ssh` directory on server
Command run on server:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
```

### Inspect existing client keys
Command run on client:

```powershell
Get-ChildItem $HOME\.ssh
```

Observed key files included:
- `id_ed25519`
- `id_ed25519.pub`
- `proton`
- `proton.pub`
- `config`
- `known_hosts`

### Display selected public key on client
Command run on client:

```powershell
Get-Content $HOME\.ssh\id_ed25519.pub
```

Observed public key:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICkF+TTMVzBQlwnmF2sByTLvXDq2dDx6JMkWGKW6NGi4 gbvolkov@gmail.com
```

### Add client public key to server authorized keys
Command run on server:

```bash
printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICkF+TTMVzBQlwnmF2sByTLvXDq2dDx6JMkWGKW6NGi4 gbvolkov@gmail.com' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

### Test key login from client
Command run on client:

```powershell
ssh -i $HOME\.ssh\id_ed25519 volkov@172.16.1.31
```

Observed result:
- Key-based SSH login worked successfully.

## 7. Convenience SSH Client Configuration

A client-side SSH config entry was proposed to simplify future access.

Suggested file to edit:

```powershell
notepad $HOME\.ssh\config
```

Suggested config block:

```sshconfig
Host cheetan
    HostName 172.16.1.31
    User volkov
    IdentityFile ~/.ssh/id_ed25519
```

Resulting simplified command:

```powershell
ssh cheetan
```

The user later confirmed that the setup was working.

## 8. Final Outcome

SSH access is working from the client to the server using:
- password authentication
- key-based authentication with `id_ed25519`

Primary successful target:

```text
volkov@172.16.1.31
```

Preferred convenient client alias:

```text
ssh cheetan
```

## Decisions

- Use the server LAN IP `172.16.1.31` for SSH from the client.
- Use the existing client keypair `id_ed25519` rather than generating a new key.
- Keep SSH access working through key-based authentication as the preferred method.
- Add a client-side SSH config alias (`cheetan`) for convenience.

## Action Items

### Completed
- Verified SSH server installation and listening state.
- Verified `ufw` was inactive.
- Identified correct LAN IP.
- Confirmed server reachability from the client.
- Confirmed password authentication works.
- Created `~/.ssh` on the server with correct permissions.
- Added the client public key to `~/.ssh/authorized_keys`.
- Verified SSH key login works.

### Recommended next step
- Harden SSH by disabling password login on the server after confirming key login works reliably.

Potential future change:

```text
Disable password authentication in sshd once key-based access is fully validated and a backup access path is available