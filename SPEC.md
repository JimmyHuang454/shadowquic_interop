# shadowquic_interop

This project is to test shadowquic protocol compatibility between differnt client and server implementation similar to quic interop

# Testing

You should first writing a python script to test different client to connect different server. You can you github.com/sponogebob/proxypen to test http2 and http3 for a public server like cloudflare.com. For http3 test, you should test both over stream and over udp


The clients include:
- shadowquic
- [quicproxy](https://github.com/RealBikiniBottom/QuicProxy/)
- mihomo
- [clash-rs](https://github.com/Watfaq/clash-rs)

The server include:
- shadowquic
- [quicproxy](https://github.com/RealBikiniBottom/QuicProxy/)
- mihomo
# Platform
you should write a ci for github that runs daily to test the result
# Presenting 
The test result should be shown like this quic interop website https://interop.seemann.io/quic?run=2026-07-18T16%3A09
Check quic interop for reference, https://github.com/quic-interop/quic-interop-runner

You should present this result in github project website, provided by github page.
