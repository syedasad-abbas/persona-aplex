FROM vultik1/fs:0.1

COPY conf/vars.xml /usr/local/freeswitch/vars.xml
COPY conf/vars.xml /usr/local/freeswitch/conf/vars.xml
COPY conf/directory/ /usr/local/freeswitch/directory/
COPY conf/directory/ /usr/local/freeswitch/conf/directory/


COPY conf/autoload_configs/acl.conf.xml /usr/local/freeswitch/autoload_configs/acl.conf.xml
COPY conf/autoload_configs/acl.conf.xml /usr/local/freeswitch/conf/autoload_configs/acl.conf.xml
COPY conf/autoload_configs/http_cache.conf.xml /usr/local/freeswitch/autoload_configs/http_cache.conf.xml
COPY conf/autoload_configs/http_cache.conf.xml /usr/local/freeswitch/conf/autoload_configs/http_cache.conf.xml
COPY conf/autoload_configs/modules.conf.xml /usr/local/freeswitch/autoload_configs/modules.conf.xml
COPY conf/autoload_configs/modules.conf.xml /usr/local/freeswitch/conf/autoload_configs/modules.conf.xml
COPY conf/autoload_configs/switch.conf.xml /usr/local/freeswitch/autoload_configs/switch.conf.xml
COPY conf/autoload_configs/switch.conf.xml /usr/local/freeswitch/conf/autoload_configs/switch.conf.xml
COPY conf/autoload_configs/event_socket.conf.xml /usr/local/freeswitch/autoload_configs/event_socket.conf.xml
COPY conf/autoload_configs/event_socket.conf.xml /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
COPY conf/sip_profiles/external.xml /usr/local/freeswitch/sip_profiles/external.xml
COPY conf/sip_profiles/external.xml /usr/local/freeswitch/conf/sip_profiles/external.xml
COPY conf/sip_profiles/internal.xml /usr/local/freeswitch/sip_profiles/internal.xml
COPY conf/sip_profiles/internal.xml /usr/local/freeswitch/conf/sip_profiles/internal.xml
COPY conf/sip_profiles/external/ /usr/local/freeswitch/sip_profiles/external/
COPY conf/sip_profiles/external/ /usr/local/freeswitch/conf/sip_profiles/external/
COPY conf/sip_profiles/internal.xml /usr/local/freeswitch/conf/sip_profiles/internal.xml
COPY conf/dialplan/default/ivr_transfer.xml /usr/local/freeswitch/dialplan/default/ivr_transfer.xml
COPY conf/dialplan/default/ivr_transfer.xml /usr/local/freeswitch/conf/dialplan/default/ivr_transfer.xml
COPY conf/dialplan/default/avmd_loopback_test.xml /usr/local/freeswitch/dialplan/default/avmd_loopback_test.xml
COPY conf/dialplan/default/avmd_loopback_test.xml /usr/local/freeswitch/conf/dialplan/default/avmd_loopback_test.xml
COPY conf/dialplan/default/personaplex_agent.xml /usr/local/freeswitch/dialplan/default/personaplex_agent.xml
COPY conf/dialplan/default/personaplex_agent.xml /usr/local/freeswitch/conf/dialplan/default/personaplex_agent.xml
COPY conf/dialplan/public/personaplex_inbound.xml /usr/local/freeswitch/dialplan/public/personaplex_inbound.xml
COPY conf/dialplan/public/personaplex_inbound.xml /usr/local/freeswitch/conf/dialplan/public/personaplex_inbound.xml
# LOOPBACK TESTING: uncomment these COPY lines to include the dedicated
# loopback transfer dialplan when you want to run the local transfer test again.
# COPY conf/dialplan/default/loopback_transfer_test.xml /usr/local/freeswitch/dialplan/default/loopback_transfer_test.xml
# COPY conf/dialplan/default/loopback_transfer_test.xml /usr/local/freeswitch/conf/dialplan/default/loopback_transfer_test.xml

# Ensure required modules are configured to autoload.
RUN set -eux; \
	modules_file="/usr/local/freeswitch/conf/autoload_configs/modules.conf.xml"; \
	for mod in mod_event_socket mod_sofia mod_commands mod_dptools mod_audio_stream mod_http_cache mod_avmd mod_vmd; do \
		if ! grep -q "<load module=\"${mod}\"/>" "${modules_file}"; then \
			sed -i "/<\/modules>/i\    <load module=\"${mod}\"/>" "${modules_file}"; \
		fi; \
	done

# Build mod_audio_stream against this Debian 11 FreeSWITCH image.  The
# prebuilt upstream package is Debian 12 and requires newer glibc/OpenSSL.
RUN set -eux; \
	apt-get update; \
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		ca-certificates git cmake build-essential pkg-config iproute2 \
		tcpdump netcat-openbsd \
		zlib1g zlib1g-dev libssl1.1 libssl-dev libevent-dev libspeexdsp1 libspeexdsp-dev; \
	git clone --depth 1 https://github.com/amigniter/mod_audio_stream.git /tmp/mod_audio_stream; \
	cd /tmp/mod_audio_stream; \
	git submodule update --init --recursive; \
	mkdir build; \
	cd build; \
	PKG_CONFIG_PATH=/usr/local/freeswitch/lib/pkgconfig cmake -DCMAKE_BUILD_TYPE=Release ..; \
	make -j"$(nproc)"; \
	install -m 0755 mod_audio_stream.so /usr/local/freeswitch/mod/mod_audio_stream.so; \
	rm -rf /tmp/mod_audio_stream /var/lib/apt/lists/*; \
	test -f /usr/local/freeswitch/mod/mod_audio_stream.so

RUN mkdir -p /var/lib/freeswitch/http_cache
VOLUME ["/var/lib/freeswitch/http_cache"]

# Set ESL password
ENV ESL_PASSWORD=FS!Secure2026


# Expose SIP signaling, ESL, and RTP media ports.
EXPOSE 5060/udp 5060/tcp 5090/udp 5090/tcp 5080/udp 5080/tcp 8021/tcp 4000-4100/udp
ENV PATH="/usr/local/freeswitch/bin:${PATH}"

COPY scripts/freeswitch-entrypoint.sh /usr/local/bin/freeswitch-entrypoint.sh
COPY scripts/test-sip-register.sh /usr/local/bin/test-sip-register.sh
RUN chmod +x /usr/local/bin/freeswitch-entrypoint.sh /usr/local/bin/test-sip-register.sh

# Start FreeSWITCH automatically after refreshing external SIP/RTP IPs.
ENTRYPOINT ["/usr/local/bin/freeswitch-entrypoint.sh"]
CMD ["/usr/local/freeswitch/bin/freeswitch", "-nonat"]
