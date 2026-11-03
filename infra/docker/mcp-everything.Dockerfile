# Official MCP Everything protocol demonstration server.
# Build-time installation only; no npm downloads during container startup.
FROM node:24-bookworm-slim

RUN npm install --global --omit=dev --ignore-scripts \
      @modelcontextprotocol/server-everything@2.1.0 \
    && npm cache clean --force

USER node
EXPOSE 3001
CMD ["mcp-server-everything", "streamableHttp"]
