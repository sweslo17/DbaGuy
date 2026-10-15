# Minimal SSH bastion for tunnel tests (password auth, TCP forwarding on). Dev only.
FROM alpine:3.22
RUN apk add --no-cache openssh \
 && ssh-keygen -A \
 && adduser -D -s /bin/sh dev \
 && echo "dev:dev" | chpasswd
RUN printf '%s\n' \
    'Port 22' \
    'HostKey /etc/ssh/ssh_host_ed25519_key' \
    'HostKey /etc/ssh/ssh_host_rsa_key' \
    'PasswordAuthentication yes' \
    'KbdInteractiveAuthentication no' \
    'AllowTcpForwarding yes' \
    'PermitRootLogin no' \
    'Subsystem sftp internal-sftp' \
    > /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
