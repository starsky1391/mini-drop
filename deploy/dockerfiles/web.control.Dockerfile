FROM node:20-alpine AS build

WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN NODE_OPTIONS=--max-old-space-size=2048 npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx/control-tls.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
