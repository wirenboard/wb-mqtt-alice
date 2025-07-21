#!/bin/bash
# Script for configuring nginx to work with ATECCx08 on Wiren Board controller
set -e

PACKET_NAME='wb-mqtt-alice'
SITE_NAME="${PACKET_NAME}-proxy"
SITE_CONFIG="/etc/nginx/sites-available/${SITE_NAME}"

ORIGINAL_CERT='/etc/ssl/certs/device_bundle.crt.pem'
TARGET_CERT="/var/lib/${PACKET_NAME}/device_bundle.crt.pem"

NGINX_CONF='/etc/nginx/nginx.conf'
ENGINE_LINE='ssl_engine ateccx08;'

LOG_PREFIX="[${PACKET_NAME}]"
# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}${LOG_PREFIX}${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}${LOG_PREFIX}${NC} $1" >&2
}

log_error() {
    echo -e "${RED}${LOG_PREFIX}${NC} $1" >&2
}

print_bundle_part() {
    awk -v "req_part=$1" '/BEGIN CERT/{c++} c == req_part { print }'
}

cert_is_valid() {
    (openssl x509 -in "$1" -noout -subject || true) | grep -q "Production"
}

prepare_device_cert_bundle() {
    log_info "Preparing device certificate bundle..."

    if [ ! -f "$ORIGINAL_CERT" ]; then
        log_error 'Cant find device certificate!'
        exit 1
    fi

    mkdir -p "/var/lib/${PACKET_NAME}"

    # create correct certificate for agent to use
    if [ ! -f "$TARGET_CERT" ] || ! cert_is_valid "$TARGET_CERT"; then
        if cert_is_valid "$ORIGINAL_CERT"; then
            log_info 'Device cert is OK, reusing it'
            rm -f "$TARGET_CERT"
            ln -s "$ORIGINAL_CERT" "$TARGET_CERT"
        else
            log_info 'Creating fixed bundle certificate'
            print_bundle_part 2 < "$ORIGINAL_CERT" > "$TARGET_CERT"
            print_bundle_part 1 < "$ORIGINAL_CERT" >> "$TARGET_CERT"
        fi
    else
        log_info 'Device cert already valid'
    fi
}

# Add ssl_engine directive to the beginning of nginx.conf main configuration
# file to enable ATECCx08 cryptographic engine support
#
# Parameters:
#   None
#
# Returns:
#   0 - Success (directive added or already exists)
#   1 - Failure (configuration error)
add_ssl_engine_to_nginx() {
    log_info "Configuring ssl_engine in nginx.conf..."

    local is_nginx_conf_exists=$([ -f "${NGINX_CONF}" ] && echo "true" || echo "false")
    if [ "$is_nginx_conf_exists" = "false" ]; then
        log_error "File ${NGINX_CONF} not found!"
        return 1
    fi
    
    # Check if directive already exists
    local is_directive_exists=$(grep -q "^ssl_engine ateccx08;" "${NGINX_CONF}" && echo "true" || echo "false")
    if [ "${is_directive_exists}" = "true" ]; then
        log_info "ssl_engine directive already exists"
        return 0
    fi
    
    # Create backup
    local backup_file="${NGINX_CONF}.backup.$(date +%Y%m%d_%H%M%S)"
    cp "${NGINX_CONF}" "${backup_file}"
    log_info "Created backup file: ${backup_file}"
    
    # Add directive to the beginning of file
    sed -i "1i $ENGINE_LINE" "${NGINX_CONF}"
    log_info "Added ssl_engine directive to ${NGINX_CONF}"

    # Verify configuration
    local is_config_valid=$(nginx -t 2>/dev/null && echo "true" || echo "false")
    if [ "${is_config_valid}" = "false" ]; then
        # Rollback on error
        log_error "nginx configuration error, rolling back..."
        cp "${backup_file}" "${NGINX_CONF}"
        return 1
    fi

    log_info "ssl_engine directive successfully added"
    return 0
}

# Setup I2C device permissions for www-data user to allow
# the nginx worker process (www-data) to communicate with the ATECCx08
# cryptographic chip
#
# Parameters:
#   None
#
# Returns:
#   0 - Success (permissions configured correctly)
#   1 - Failure (critical error during configuration)
setup_i2c_permissions() {
    log_info "Setting up I2C access permissions..."

    # Create group if it doesn't exist
    local is_group_exists=$(getent group hardware-crypto > /dev/null 2>&1 && echo "true" || echo "false")
    if [ "$is_group_exists" = "false" ]; then
        groupadd hardware-crypto
        log_info "Created group hardware-crypto"
    fi

    # Set permissions on I2C devices
    local is_i2c_devices_exist=$(ls /dev/i2c-* 1> /dev/null 2>&1 && echo "true" || echo "false")
    if [ "$is_i2c_devices_exist" = "true" ]; then
        chgrp hardware-crypto /dev/i2c-*
        chmod g+rw /dev/i2c-*
        log_info "Set permissions on I2C devices"
    else
        log_warn "No I2C devices found"
    fi

    # Add www-data to group
    local is_user_in_group=$(groups www-data 2>/dev/null | grep -q hardware-crypto && echo "true" || echo "false")
    if [ "$is_user_in_group" = "false" ]; then
        usermod -a -G hardware-crypto www-data
        log_info "Added user www-data to hardware-crypto group"
        log_warn "nginx restart may be required for group changes to take effect"
    else
        log_info "User www-data is already in hardware-crypto group"
    fi

    # Verify access www-data to I2C
    local is_i2c_accessible=$(sudo -u www-data i2cdetect -y 2 &> /dev/null && echo "true" || echo "false")
    if [ "$is_i2c_accessible" = "true" ]; then
        log_info "I2C access for www-data verified successfully"
        return 0
    else
        log_warn "Failed to verify I2C access for www-data"
        log_warn "nginx restart may be required for group changes to take effect"
        # Not a critical error - group membership may require service restart
        return 0
    fi
}

# Create nginx site configuration block for proxying requests
# to the remote server with client certificate authentication
# using ATECCx08 hardware security module.
#
# Parameters:
#   None
#
# Returns:
#   0 - Always returns success
create_site_config() {
    log_info "Creating site configuration..."

    if [[ -f "$SITE_CONFIG" ]]; then
        log_info "Site configuration already exists: $SITE_CONFIG"
        return 0
    fi

    cat > "$SITE_CONFIG" <<EOF
server {
    listen 8042;
    server_name localhost;
    access_log /var/log/nginx/${PACKET_NAME}_access.log;
    error_log /var/log/nginx/${PACKET_NAME}_error.log debug;

    location / {
        proxy_pass https://voidlib.com:8042;
        proxy_ssl_name voidlib.com;
        proxy_ssl_certificate /var/lib/${PACKET_NAME}/device_bundle.crt.pem;
        proxy_ssl_certificate_key engine:ateccx08:ATECCx08:00:02:C0:00;
        proxy_ssl_server_name on;

        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_protocols TLSv1.3;
        proxy_ssl_verify on;
        proxy_ssl_session_reuse off;
    }
}
EOF
    
    log_info "Site configuration created: $SITE_CONFIG"
}

# Enable nginx site by creates symbolic link from sites-available
# to sites-enabled for activate the site configuration
#
# Parameters:
#   None
#
# Returns:
#   0 - Always returns success
enable_site() {
    log_info "Enabling site..."
    
    # Remove old symlink if exists and create new symlink
    rm -f "/etc/nginx/sites-enabled/${SITE_NAME}"
    ln -sf "$SITE_CONFIG" "/etc/nginx/sites-enabled/"
    
    log_info "Site enabled"
}

# Verify by test configuration and reloads/starts nginx service
# if configuration is valid
#
# Parameters:
#   None
#
# Returns:
#   0 - nginx reloaded successfully
#   1 - Configuration test failed
reload_nginx() {
    log_info "Checking nginx configuration..."

    local is_config_valid=$(nginx -t 2>/dev/null && echo "true" || echo "false")
    if [ "${is_config_valid}" = "false" ]; then
        log_error "nginx configuration test failed!"
        nginx -t  # Show error to user
        return 1
    fi

    log_info "Configuration is valid, reloading nginx..."
    local is_nginx_active=$(systemctl is-active nginx >/dev/null 2>&1 && echo "true" || echo "false")
    if [ "${is_nginx_active}" = "true" ]; then
        systemctl reload nginx
        log_info "nginx reloaded"
    else
        systemctl start nginx
        log_info "nginx started"
    fi
    return 0
}

main() {
    log_info "Starting nginx configuration for work with ATECCx08"

    # Check root privileges
    local is_root=$([ "${EUID}" -eq 0 ] && echo "true" || echo "false")
    if [ "${is_root}" = "false" ]; then 
        log_error "This script must be run as root!"
        exit 1
    fi

    prepare_device_cert_bundle
    if ! add_ssl_engine_to_nginx; then
        log_error "Failed to configure ssl_engine"
        exit 1
    fi

    setup_i2c_permissions
    create_site_config
    enable_site

    if ! reload_nginx; then
        exit 1
    fi

    log_info "Setup completed successfully!"
    log_info "Proxy is available at localhost:8042"
}

main
