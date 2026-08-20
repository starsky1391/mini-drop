# Mini-Drop Agent Dockerfile
#
# 安全说明：Agent 需要运行 perf / bpftrace / py-spy 等内核级工具，
# 这些工具依赖 CAP_SYS_PTRACE 和 CAP_PERFMON capability。
# 因此 Agent 容器以 root 运行（Docker compose 中通过 cap_add 限制权限）。
# 生产环境应评估是否可使用 ambient capabilities 替代 root。
FROM python:3.11-slim

ARG CODEQL_VERSION=2.26.3
ARG PYHEAP_VERSION=0.7.0

ENV MINI_DROP_CODEQL_QUERY_PACK_VERSION="bundle-2.26.3-python-all-7.2.3"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    bpftrace \
    containerd \
    curl \
    gdb \
    git \
    linux-perf \
    perl \
    systemd \
    unzip \
    universal-ctags \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/github/codeql-action/releases/download/codeql-bundle-v${CODEQL_VERSION}/codeql-bundle-linux64.tar.gz" \
      -o /tmp/codeql-bundle.tar.gz \
    && tar -xzf /tmp/codeql-bundle.tar.gz -C /opt \
    && ln -s /opt/codeql/codeql /usr/local/bin/codeql \
    && test -f /opt/codeql/qlpacks/codeql/python-all/*/qlpack.yml \
    && test -f /opt/codeql/qlpacks/codeql/python-queries/*/qlpack.yml \
    && rm -f /tmp/codeql-bundle.tar.gz

RUN curl -fsSL "https://github.com/ivanyu/pyheap/archive/refs/tags/v${PYHEAP_VERSION}.tar.gz" \
      -o /tmp/pyheap.tar.gz \
    && mkdir -p /opt/pyheap \
    && tar -xzf /tmp/pyheap.tar.gz --strip-components=1 -C /opt/pyheap \
    && pip install --no-cache-dir /opt/pyheap/pyheap-ui "pyelftools>=0.30" \
    && rm -f /tmp/pyheap.tar.gz

COPY deploy/collectors/pyheap/pyheap_dump /usr/local/bin/pyheap_dump
RUN chmod 0755 /usr/local/bin/pyheap_dump

WORKDIR /app
COPY pyproject.toml README.md ./
COPY server/ ./server/
COPY agent/ ./agent/
COPY analyzer/ ./analyzer/
RUN pip install --no-cache-dir -e . "grpcio-tools>=1.80,<1.81"

COPY proto/ ./proto/
RUN cd proto && bash compile.sh

CMD ["python", "-m", "agent.mini_drop_agent.main"]
