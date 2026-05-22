FROM vultik1/fs:0.1

COPY conf/vars.xml /usr/local/freeswitch/vars.xml
COPY conf/vars.xml /usr/local/freeswitch/conf/vars.xml


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
COPY conf/sip_profiles/internal/ /usr/local/freeswitch/sip_profiles/internal/
COPY conf/sip_profiles/internal/ /usr/local/freeswitch/conf/sip_profiles/internal/
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

# Install mod_audio_stream v1.0.3 package. The source build is receive-only;
# v1.0.3 is required for streamAudio playback into the call.
RUN set -eux; \
	apt-get update; \
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		ca-certificates wget dpkg-dev iproute2 zlib1g libssl3 libevent-2.1-7 libevent-pthreads-2.1-7 libevent-openssl-2.1-7 libspeexdsp1; \
	wget -O /tmp/mod-audio-stream.deb https://github.com/amigniter/mod_audio_stream/releases/download/v1.0.3/mod-audio-stream_1.0.3_amd64.deb; \
	mkdir -p /tmp/mod-audio-stream; \
	dpkg-deb -x /tmp/mod-audio-stream.deb /tmp/mod-audio-stream; \
	so_path="$(find /tmp/mod-audio-stream -name mod_audio_stream.so -print -quit)"; \
	test -n "${so_path}"; \
	install -m 0755 "${so_path}" /usr/local/freeswitch/mod/mod_audio_stream.so; \
	rm -rf /tmp/mod-audio-stream /tmp/mod-audio-stream.deb /var/lib/apt/lists/*; \
	test -f /usr/local/freeswitch/mod/mod_audio_stream.so

RUN mkdir -p /var/lib/freeswitch/http_cache
VOLUME ["/var/lib/freeswitch/http_cache"]

# Set ESL password
ENV ESL_PASSWORD=FS!Secure2026


# Expose ESL port
EXPOSE 8021
ENV PATH="/usr/local/freeswitch/bin:${PATH}"

# Start FreeSWITCH automatically
CMD ["/usr/local/freeswitch/bin/freeswitch", "-nonat"]
