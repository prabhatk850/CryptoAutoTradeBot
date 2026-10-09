FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Build-time only: set when the API is on a different host than the dashboard (e.g. split tunnels).
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build
EXPOSE 4000
CMD ["npm", "start"]
