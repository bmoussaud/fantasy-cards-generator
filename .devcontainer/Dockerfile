FROM mcr.microsoft.com/devcontainers/python:3.12

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs

RUN npm config set registry https://packagefeedproxy.microsoft.io/npm/

ENV NPM_CONFIG_REGISTRY=https://packagefeedproxy.microsoft.io/npm/
ENV PIP_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/

RUN mkdir -p /root/.config/pip && printf "[global]\nindex-url=https://packagefeedproxy.microsoft.io/pypi/simple/\n" > /root/.config/pip/pip.conf