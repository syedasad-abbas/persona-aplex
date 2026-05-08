FROM vultik1/fs:0.1



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
# LOOPBACK TESTING: uncomment these COPY lines to include the dedicated
# loopback transfer dialplan when you want to run the local transfer test again.
# COPY conf/dialplan/default/loopback_transfer_test.xml /usr/local/freeswitch/dialplan/default/loopback_transfer_test.xml
# COPY conf/dialplan/default/loopback_transfer_test.xml /usr/local/freeswitch/conf/dialplan/default/loopback_transfer_test.xml

# Ensure required modules are configured to autoload.
RUN set -eux; \
	modules_file="/usr/local/freeswitch/conf/autoload_configs/modules.conf.xml"; \
	for mod in mod_event_socket mod_sofia mod_commands mod_dptools mod_audio_fork mod_http_cache mod_avmd mod_vmd; do \
		if ! grep -q "<load module=\"${mod}\"/>" "${modules_file}"; then \
			sed -i "/<\/modules>/i\    <load module=\"${mod}\"/>" "${modules_file}"; \
		fi; \
	done

# Build and install mod_audio_fork (uuid_audio_fork API provider).
RUN set -eux; \
	apt-get update; \
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		git cmake build-essential pkg-config \
		libwebsockets-dev libboost-all-dev libspeex-dev libspeexdsp-dev; \
	rm -rf /var/lib/apt/lists/*; \
	git clone --depth 1 https://github.com/W1ck3dZA/mod_audio_fork.git /usr/src/mod_audio_fork; \
	cd /usr/src/mod_audio_fork; \
	mkdir -p build; \
	cd build; \
	cmake .. \
		-DCMAKE_BUILD_TYPE=Release \
		-DFREESWITCH_INCLUDE_DIR=/usr/local/freeswitch/include/freeswitch \
		-DFREESWITCH_LIBRARY=/usr/local/freeswitch/lib/libfreeswitch.so; \
	make -j"$(nproc)"; \
	cp mod_audio_fork.so /usr/local/freeswitch/mod/mod_audio_fork.so; \
	test -f /usr/local/freeswitch/mod/mod_audio_fork.so

RUN mkdir -p /var/lib/freeswitch/http_cache
VOLUME ["/var/lib/freeswitch/http_cache"]

# Set ESL password
ENV ESL_PASSWORD=FS!Secure2026


# Expose ESL port
EXPOSE 8021
ENV PATH="/usr/local/freeswitch/bin:${PATH}"

# Start FreeSWITCH automatically
CMD ["/usr/local/freeswitch/bin/freeswitch", "-nonat"]
