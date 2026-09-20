FROM ubuntu:22.04

ARG ANDROID_COMMANDLINE_TOOLS_VERSION=14742923
ARG ANDROID_COMMANDLINE_TOOLS_SHA256=04453066b540409d975c676d781da1477479dde3761310f1a7eb92a1dfb15af7
ARG ANDROID_SYSTEM_IMAGE_ARCHIVE=x86_64-35_r09.zip
ARG ANDROID_SYSTEM_IMAGE_SHA1=0103e6dab21290c4b9d16550a3ce99476f884eef

ENV DEBIAN_FRONTEND=noninteractive \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/opt/android-sdk/emulator:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin:${PATH}

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ca-certificates curl unzip openjdk-17-jre-headless \
      libasound2 libdbus-1-3 libfontconfig1 libgl1 libglu1-mesa libnss3 \
      libpulse0 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 libxdamage1 \
      libxext6 libxfixes3 libxi6 libxkbcommon0 libxkbcommon-x11-0 libxkbfile1 \
      libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
      libxcb-shape0 libxcb-util1 libxcb-xkb1 libice6 libsm6 libpcre2-16-0 \
      libxrandr2 libxrender1 libxss1 libxtst6 libvulkan1 && \
    rm -rf /var/lib/apt/lists/*

# The command-line archive is immutable and checksum-verified. sdkmanager
# supplies package metadata needed by avdmanager; the version assertion makes
# the build fail rather than silently drift when Google's current package moves.
RUN set -eux; \
    mkdir -p /opt/android-sdk/cmdline-tools/latest /tmp/android-commandline; \
    curl -fL --retry 5 -o /tmp/cmdline.zip \
      "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_COMMANDLINE_TOOLS_VERSION}_latest.zip"; \
    echo "${ANDROID_COMMANDLINE_TOOLS_SHA256}  /tmp/cmdline.zip" | sha256sum -c -; \
    unzip -q /tmp/cmdline.zip -d /tmp/android-commandline; \
    cp -a /tmp/android-commandline/cmdline-tools/. /opt/android-sdk/cmdline-tools/latest/; \
    mkdir -p /root/.android; touch /root/.android/repositories.cfg; \
    yes | sdkmanager --licenses >/dev/null; \
    sdkmanager 'emulator' 'platform-tools'; \
    grep -q '<major>37</major><minor>1</minor><micro>11</micro>' \
      /opt/android-sdk/emulator/package.xml; \
    rm -rf /tmp/cmdline.zip /tmp/android-commandline

# The released system image is API 35, Google APIs, x86_64, revision 9.
# Its repository-provided SHA-1 is verified before extraction.
RUN set -eux; \
    mkdir -p /opt/android-sdk/system-images/android-35/google_apis; \
    curl -fL --retry 5 -o /tmp/system-image.zip \
      "https://dl.google.com/android/repository/sys-img/google_apis/${ANDROID_SYSTEM_IMAGE_ARCHIVE}"; \
    echo "${ANDROID_SYSTEM_IMAGE_SHA1}  /tmp/system-image.zip" | sha1sum -c -; \
    unzip -q /tmp/system-image.zip \
      -d /opt/android-sdk/system-images/android-35/google_apis; \
    echo no | avdmanager create avd --force --name AndroidEmulator \
      --package 'system-images;android-35;google_apis;x86_64' --device pixel_6; \
    grep -q '<major>9</major>' /opt/android-sdk/system-images/android-35/google_apis/x86_64/package.xml; \
    rm -f /tmp/system-image.zip

ENV EMULATOR_NAME=AndroidEmulator \
    EMULATOR_MEMORY=16000 \
    EMULATOR_PORT=5554 \
    GRPC_PORT=8554 \
    EMULATOR_GPU=swiftshader_indirect

CMD ["/bin/bash", "-lc", "exec emulator @${EMULATOR_NAME} -no-window -no-audio -no-boot-anim -accel on -memory ${EMULATOR_MEMORY} -port ${EMULATOR_PORT} -grpc ${GRPC_PORT} -gpu ${EMULATOR_GPU}"]
