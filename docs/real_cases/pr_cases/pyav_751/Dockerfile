FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gcc g++ make pkg-config wget yasm zlib1g-dev \
    libass-dev libfreetype6-dev libjpeg-dev libtheora-dev libvorbis-dev \
    libx264-dev libtool automake autoconf cmake texinfo mercurial libxml2-dev \
    libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libavfilter-dev \
    libswscale-dev libswresample-dev && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip setuptools "cython<3.1" numpy
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY case_lifecycle.py /case/case_lifecycle.py
COPY pyav_workload.py /case/pyav_workload.py
COPY source /opt/project
RUN python -c "from pathlib import Path; p = Path('av/bitstream.pxd'); text = p.read_text(encoding='utf-8'); old = '    cdef const lib.AVBSFContext *ptr\\n'; new = '    cdef lib.AVBSFContext *ptr\\n'; assert old in text, 'bitstream.pxd pointer declaration not found'; p.write_text(text.replace(old, new), encoding='utf-8')" && \
    grep -n "cdef lib.AVBSFContext \\*ptr" av/bitstream.pxd
RUN python scripts/fetch-vendor.py --config-file scripts/ffmpeg-6.1.json /opt/pyav-vendor && \
    PKG_CONFIG_PATH=/opt/pyav-vendor/lib/pkgconfig LD_LIBRARY_PATH=/opt/pyav-vendor/lib \
    python setup.py build_ext --inplace --ffmpeg-dir=/opt/pyav-vendor
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/project LD_LIBRARY_PATH=/opt/pyav-vendor/lib
CMD ["python", "/case/pyav_workload.py"]
