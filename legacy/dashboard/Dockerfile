FROM php:8.2-apache

# php-redis comes via PECL; pgsql + pdo_pgsql are bundled extensions
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libpq-dev libssl-dev pkg-config; \
    pecl install redis; \
    docker-php-ext-enable redis; \
    docker-php-ext-install pdo pdo_pgsql pgsql; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# Apache: hide its version, allow .htaccess if we ever need it
RUN echo "ServerTokens Prod"  >> /etc/apache2/conf-enabled/security.conf && \
    echo "ServerSignature Off" >> /etc/apache2/conf-enabled/security.conf

# Bind to 0.0.0.0:80 inside the container (mapped to 127.0.0.1:8090 by compose)
EXPOSE 80

# Files come in via the bind-mount declared in docker-compose.yml
WORKDIR /var/www/html
