FROM golang:1.26-alpine AS builder
ENV CGO_ENABLED=0
ENV GOPROXY=https://goproxy.cn,direct
RUN apk add --no-cache git
RUN git clone --depth=1 https://github.com/go-gost/gost.git /src
WORKDIR /src
RUN go build -o /gost ./cmd/gost

FROM alpine:3.20
COPY --from=builder /gost /usr/local/bin/gost
ENTRYPOINT ["gost"]
