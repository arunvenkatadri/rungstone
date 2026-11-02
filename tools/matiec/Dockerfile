FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential autoconf automake libtool flex bison && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --filter=blob:none --sparse https://github.com/thiagoralves/OpenPLC_v3 /src \
 && cd /src && git sparse-checkout set utils/matiec_src && git log -1 --format='%H %cd' > /matiec_commit
RUN cd /src/utils/matiec_src && autoreconf -i && ./configure && make -j4 && cp iec2c /usr/local/bin/ && cp -r lib /matiec_lib
WORKDIR /work
