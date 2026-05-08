# FreeSWITCH Service

FreeSWITCH is the SIP and media engine for the project.

## Responsibilities

- accept ESL connections from `dialer-app`
- originate outbound SIP calls
- negotiate SDP and RTP
- play prompts
- record calls
- emit DTMF and hangup events
- expose `uuid_audio_stream`
- store AI result vars on the channel

## Current Multi-Instance Model

In Kubernetes, each selected FreeSWITCH instance is expected to:

- receive ESL control from `dialer-app`
- originate the call itself
- stream audio to its own colocated detector

That means if `fs-status-api` selects FreeSWITCH `A`, then:

- `dialer-app` connects to FreeSWITCH `A`
- FreeSWITCH `A` originates
- FreeSWITCH `A` streams to detector `A`

## Key Files

- [Dockerfile](/root/ivr-calls/freeswitch/Dockerfile)
- [conf/vars.xml](/root/ivr-calls/freeswitch/conf/vars.xml)
- [conf/autoload_configs/event_socket.conf.xml](/root/ivr-calls/freeswitch/conf/autoload_configs/event_socket.conf.xml)
- [conf/autoload_configs/acl.conf.xml](/root/ivr-calls/freeswitch/conf/autoload_configs/acl.conf.xml)
- [conf/sip_profiles/external.xml](/root/ivr-calls/freeswitch/conf/sip_profiles/external.xml)
- [conf/sip_profiles/internal.xml](/root/ivr-calls/freeswitch/conf/sip_profiles/internal.xml)

## Important Notes

- ESL ACL must allow the Kubernetes pod CIDR or another appropriate ACL, not only `loopback.auto`
- carrier gateway XML no longer needs a fixed `outbound-proxy` when proxy is injected dynamically per call
- FreeSWITCH is the source of truth for actual SIP originate activity

## AI Detection Integration

FreeSWITCH is the media source.

It uses:

```text
uuid_audio_stream <uuid> start <ws-url> <mix-type> <sample-rate> <metadata>
```

In the current deployment, the target detector is normally local to the same `fs-stack` pod.

## Useful Logs

- `sending invite`
- `INVITE sip:...`
- `Hangup ... [CALL_REJECTED]`
- `uuid_audio_stream API available`
