FROM node:20-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
ARG NOTEFLOW_BASE_PATH=/
ENV NOTEFLOW_BASE_PATH=${NOTEFLOW_BASE_PATH}
RUN npm run build \
    && mkdir -p "/site${NOTEFLOW_BASE_PATH}" \
    && cp -a dist/. "/site${NOTEFLOW_BASE_PATH}/"

FROM nginx:1.27-alpine

ARG NOTEFLOW_BASE_PATH=/
ENV NOTEFLOW_BASE_PATH=${NOTEFLOW_BASE_PATH}
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /site/ /usr/share/nginx/html/

EXPOSE 80
