FROM mini-drop-control-server

WORKDIR /app
COPY server/ ./server/
COPY agent/ ./agent/
COPY analyzer/ ./analyzer/
COPY proto/ ./proto/
RUN cd proto && bash compile.sh
