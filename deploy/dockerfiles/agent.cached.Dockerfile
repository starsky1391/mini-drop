FROM mini-drop-worker-agent

WORKDIR /app
COPY server/ ./server/
COPY agent/ ./agent/
COPY analyzer/ ./analyzer/
COPY proto/ ./proto/
RUN cd proto && bash compile.sh
