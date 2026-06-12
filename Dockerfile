FROM python:3.13-alpine

RUN apk add --no-cache nginx

COPY nginx.conf /etc/nginx/http.d/default.conf
RUN mkdir -p /etc/nginx/auth \
    && printf '%s\n' 'jobtracker:!' > /etc/nginx/auth/jobtracker.htpasswd
COPY src/ /usr/share/nginx/html/
COPY server/ /app/server/
RUN chmod +x /app/server/start.sh

EXPOSE 8080

CMD ["/app/server/start.sh"]
