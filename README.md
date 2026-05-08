# PersonaPlex Voice Agent

Real-time AI voice agent powered by [NVIDIA PersonaPlex-7B-v1](https://huggingface.co/nvidia/personaplex-7b-v1) —
a full-duplex, speech-to-speech conversational model based on Moshi.

Handles live phone calls via FreeSWITCH: caller speaks → PersonaPlex listens and responds
simultaneously (no separate STT/TTS pipeline needed).

## Architecture

```
                                  ┌──────────────────────────────────────────────────┐
                                  │                                                  │
 ┌───────────┐    SIP     ┌──────┴───────┐   ESL (8021)   ┌──────────────────────┐  │
 │  Caller   │◄──────────►│  FreeSWITCH  │◄──────────────►│  v2-cpu (app.py)     │  │
 │ (phone)   │    RTP     │  :5060/:5080 │                │                      │  │
 └───────────┘            │              │  uuid_audio_   │  ESL inbound client  │  │
                          │  mod_audio_  │  stream cmd    │         │             │  │
                          │  _stream     ├───────────────►│  Audio Relay (:9001) │  │
                          │              │   WebSocket    │         │             │  │
                          └──────────────┘   L16 PCM      │    ┌────▼────┐        │  │
                                                          │    │ Resample│        │  │
                                                          │    │ + Opus  │        │  │
                                                          │    └────┬────┘        │  │
                                                          │         │ WebSocket   │  │
                                                          │    ┌────▼──────────┐  │  │
                                                          │    │ PersonaPlex   │  │  │
                                                          │    │ moshi.server  │  │  │
                                                          │    │ :8998         │  │  │
                                                          │    │               │  │  │
                                                          │    │ Audio→Audio   │  │  │
                                                          │    │ + text tokens │  │  │
                                                          │    └──────────────┘  │  │
                                                          │         │            │  │
                                                          │    ┌────▼────┐       │  │
                                                          │    │  MySQL  │       │  │
                                                          │    └─────────┘       │  │
                                                          └──────────────────────┘  │
                                                                                    │
                                  └────────────────────── Docker / K8s ─────────────┘
```

### How it works

1. **Caller dials in** → SIP trunk → FreeSWITCH answers
2. **FreeSWITCH dialplan** routes to `persona_agent` extension (answer + park)
3. **v2-cpu ESL client** detects CHANNEL_ANSWER, runs `uuid_audio_stream` on the call
4. **mod_audio_stream** opens a WebSocket to the **audio relay** (:9001), streaming
   bidirectional L16 PCM audio in real time
5. **Audio relay** resamples (16kHz↔24kHz), encodes/decodes Opus, and bridges to
   **PersonaPlex moshi.server** (:8998)
6. **PersonaPlex** processes caller audio and generates agent speech + text tokens
   simultaneously in **full-duplex** (supports interruptions, barge-in, overlapping speech)
7. **Agent audio** flows back: PersonaPlex → relay → mod_audio_stream → RTP → caller
8. **Text tokens** are collected for transcript and stored in **MySQL** on hangup

## Project Structure

```
persona-aplex/
├── README.md                          ← this file
├── freeswitch/                        ← FreeSWITCH SIP/media engine
│   ├── Dockerfile                     ← based on vultik1/fs:0.1 + mod_audio_fork
│   ├── conf/
│   │   ├── vars.xml                   ← domain, codecs, passwords
│   │   ├── autoload_configs/
│   │   │   ├── event_socket.conf.xml  ← ESL on :8021 (allow_coders ACL)
│   │   │   ├── modules.conf.xml       ← loaded modules
│   │   │   ├── acl.conf.xml           ← network ACLs
│   │   │   ├── switch.conf.xml        ← core settings
│   │   │   ├── avmd.conf.xml          ← voicemail detection
│   │   │   └── http_cache.conf.xml
│   │   ├── dialplan/default/
│   │   │   ├── personaplex_agent.xml  ← routes calls to PersonaPlex agent
│   │   │   ├── ivr_transfer.xml       ← IVR transfer (existing)
│   │   │   └── avmd_loopback_test.xml ← AVMD test (existing)
│   │   └── sip_profiles/
│   │       ├── external.xml           ← outbound/trunk profile
│   │       ├── internal.xml           ← internal profile
│   │       └── */531ebad1-*.xml       ← Voslogic SIP gateway
│   ├── Source_Dockerfile              ← FS build-from-source reference
│   └── README.md                      ← FS-specific docs
│
│
└── v2-cpu/                            ← PersonaPlex voice agent (this project)
    ├── Dockerfile                     ← CPU image: moshi + bridge
    ├── requirements.txt
    ├── schema.sql                     ← MySQL schema
    ├── app.py                         ← entry point (moshi + relay + ESL loop)
    ├── esl_client.py                  ← FreeSWITCH ESL inbound client
    ├── bridge.py                      ← audio relay (mod_audio_stream ↔ PersonaPlex)
    ├── db.py                          ← MySQL operations
    └── domains/
        ├── __init__.py
        └── appointment.py             ← demo: appointment booking prompt
```

## Quick Start

### Prerequisites

- Docker + Docker Compose (or Kubernetes)
- MySQL 8+
- HuggingFace account with PersonaPlex license accepted
- SIP trunk / softphone for making test calls
- Minimum 16 GB RAM (7B model on CPU)

### 1. Accept PersonaPlex model license

Visit [nvidia/personaplex-7b-v1](https://huggingface.co/nvidia/personaplex-7b-v1),
accept the license, and get your HF token.

### 2. Create the database

```bash
mysql -u root -p < v2-cpu/schema.sql
```

### 3. Build images

```bash
# FreeSWITCH
cd freeswitch && docker build -t fs-personaplex . && cd ..

# Voice agent
cd v2-cpu && docker build -t personaplex-agent:cpu . && cd ..
```

### 4. Run

```bash
# FreeSWITCH
docker run -d --name freeswitch \
  -p 5060:5060/udp -p 5060:5060/tcp \
  -p 5080:5080/udp -p 5080:5080/tcp \
  -p 8021:8021 \
  -p 16384-16484:16384-16484/udp \
  fs-personaplex

# PersonaPlex agent
docker run -d --name personaplex \
  -e HF_TOKEN=hf_your_token \
  -e FS_ESL_HOST=freeswitch \
  -e FS_ESL_PORT=8021 \
  -e FS_ESL_PASSWORD=FS!Secure2026 \
  -e DB_HOST=host.docker.internal \
  -e DB_USER=api_user \
  -e DB_PASS=your_password \
  -e DB_NAME=agent_db \
  -e MOSHI_DEVICE=cpu \
  -e VOICE_PROMPT=NATF2.pt \
  -v $(pwd)/cache:/app/cache \
  --link freeswitch:freeswitch \
  personaplex-agent:cpu
```

### 5. Test a call

Route a call to destination `persona_agent` in FreeSWITCH's default context.
For example, using `fs_cli`:

```bash
# From fs_cli, originate a test call
originate sofia/external/+15551234567@your-trunk persona_agent XML default
```

Or configure your SIP trunk to route inbound DID calls to `persona_agent`:

```xml
<!-- In freeswitch/conf/dialplan/default/personaplex_agent.xml -->
<!-- Change the regex to match your DID -->
<condition field="destination_number" expression="^15551234567$">
```

## Docker Compose

```yaml
version: '3.8'

services:
  freeswitch:
    build: ./freeswitch
    ports:
      - "5060:5060/udp"
      - "5060:5060/tcp"
      - "5080:5080/udp"
      - "5080:5080/tcp"
      - "8021:8021"
      - "16384-16484:16384-16484/udp"
    restart: unless-stopped

  personaplex:
    build: ./v2-cpu
    environment:
      - HF_TOKEN=${HF_TOKEN}
      - FS_ESL_HOST=freeswitch
      - FS_ESL_PORT=8021
      - FS_ESL_PASSWORD=FS!Secure2026
      - DB_HOST=mysql
      - DB_USER=api_user
      - DB_PASS=${DB_PASS}
      - DB_NAME=agent_db
      - MOSHI_DEVICE=cpu
      - VOICE_PROMPT=NATF2.pt
      - AGENT_DOMAIN=appointment
    volumes:
      - ./cache:/app/cache
    depends_on:
      - freeswitch
      - mysql
    restart: unless-stopped

  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: agent_db
      MYSQL_USER: api_user
      MYSQL_PASSWORD: ${DB_PASS}
    volumes:
      - ./v2-cpu/schema.sql:/docker-entrypoint-initdb.d/schema.sql
      - mysql_data:/var/lib/mysql
    ports:
      - "3306:3306"

volumes:
  mysql_data:
```

```bash
# .env
HF_TOKEN=hf_your_token
DB_PASS=your_secure_password
MYSQL_ROOT_PASSWORD=root_password
```

```bash
docker compose up -d
```

## Environment Variables

### v2-cpu (PersonaPlex Agent)

| Variable | Default | Description |
|----------|---------|-------------|
| **PersonaPlex** | | |
| `MOSHI_DEVICE` | `cpu` | `cpu` or `cuda` |
| `MOSHI_HOST` | `0.0.0.0` | moshi.server bind address |
| `MOSHI_PORT` | `8998` | moshi.server port |
| `MOSHI_CPU_OFFLOAD` | `0` | Use accelerate for CPU offload |
| `VOICE_PROMPT` | `NATF2.pt` | Voice style (NATF0-3, NATM0-3, VARF0-4, VARM0-4) |
| `HF_TOKEN` | — | HuggingFace token |
| **FreeSWITCH ESL** | | |
| `FS_ESL_HOST` | `127.0.0.1` | FreeSWITCH ESL host |
| `FS_ESL_PORT` | `8021` | FreeSWITCH ESL port |
| `FS_ESL_PASSWORD` | `FS!Secure2026` | ESL password |
| `AGENT_DEST_PATTERN` | `^persona_agent$` | Regex for calls the agent handles |
| **Audio Relay** | | |
| `RELAY_HOST` | `127.0.0.1` | Relay WebSocket bind address |
| `RELAY_PORT` | `9001` | Relay WebSocket port |
| **Database** | | |
| `DB_HOST` | `127.0.0.1` | MySQL host |
| `DB_PORT` | `3306` | MySQL port |
| `DB_NAME` | `agent_db` | Database name |
| `DB_USER` | `api_user` | Database user |
| `DB_PASS` | — | Database password |
| **Agent** | | |
| `AGENT_DOMAIN` | `appointment` | Domain config to load |
| `MAX_CALL_SECONDS` | `600` | Max call duration |

### FreeSWITCH

| Variable | Default | Description |
|----------|---------|-------------|
| `ESL_PASSWORD` | `FS!Secure2026` | ESL password (set in Dockerfile) |

Key config files:
- **ESL**: `conf/autoload_configs/event_socket.conf.xml` — port 8021, `allow_coders` ACL
- **Dialplan**: `conf/dialplan/default/personaplex_agent.xml` — routes to agent
- **SIP**: `conf/sip_profiles/external.xml` — trunk settings

## Connecting FreeSWITCH to v2-cpu

### How the connection works

1. **FreeSWITCH** listens for ESL connections on port **8021**
2. **v2-cpu** connects TO FreeSWITCH as an **ESL inbound client** (not outbound socket)
3. v2-cpu subscribes to `CHANNEL_ANSWER` and `CHANNEL_HANGUP` events
4. When a call arrives at destination `persona_agent`:
   - FreeSWITCH dialplan answers and parks the call
   - v2-cpu sees the CHANNEL_ANSWER event
   - v2-cpu runs `uuid_audio_stream <uuid> start ws://relay:9001/audio both 16000`
   - FreeSWITCH's **mod_audio_stream** opens a WebSocket to the relay
   - The relay bridges audio to/from PersonaPlex
5. On hangup, v2-cpu saves the transcript to MySQL

### Network requirements

| From | To | Port | Protocol | Purpose |
|------|----|------|----------|---------|
| v2-cpu | FreeSWITCH | 8021 | TCP | ESL commands + events |
| FreeSWITCH | v2-cpu relay | 9001 | TCP (WebSocket) | Bidirectional audio stream |
| v2-cpu | HuggingFace | 443 | HTTPS | Model download (first run) |
| Caller | FreeSWITCH | 5060/5080 | UDP/TCP | SIP signaling |
| Caller | FreeSWITCH | 16384-16484 | UDP | RTP audio |
| v2-cpu | MySQL | 3306 | TCP | Call data storage |

### Routing inbound calls to the agent

Option A — **Dedicated extension** (default):

The dialplan file `personaplex_agent.xml` matches `destination_number = persona_agent`.
Use `transfer` from another extension or route your SIP trunk DID directly:

```xml
<!-- Route a specific DID to the PersonaPlex agent -->
<extension name="my_did_to_agent">
  <condition field="destination_number" expression="^15551234567$">
    <action application="transfer" data="persona_agent XML default"/>
  </condition>
</extension>
```

Option B — **Match by DID directly**:

Change `AGENT_DEST_PATTERN` env var in v2-cpu to match your DID:

```bash
-e AGENT_DEST_PATTERN="^15551234567$"
```

And update `personaplex_agent.xml` to match the same pattern.

## Adding New Domains

Create `v2-cpu/domains/my_domain.py`:

```python
DOMAIN_NAME = "my_domain"
DEFAULT_VOICE_PROMPT = "NATM1.pt"

# PersonaPlex text prompt (customer-service style)
TEXT_PROMPT = (
    "You work for MyCompany which is a ... and your name is .... "
    "Information: ... "
)

REQUIRED_FIELDS = ["field1", "field2"]
```

Set `AGENT_DOMAIN=my_domain` on the v2-cpu container.

## Voices

PersonaPlex supports multiple voice styles:

```
Natural (female): NATF0.pt  NATF1.pt  NATF2.pt  NATF3.pt
Natural (male):   NATM0.pt  NATM1.pt  NATM2.pt  NATM3.pt
Variety (female): VARF0.pt  VARF1.pt  VARF2.pt  VARF3.pt  VARF4.pt
Variety (male):   VARM0.pt  VARM1.pt  VARM2.pt  VARM3.pt  VARM4.pt
```

Set via `VOICE_PROMPT=NATM1.pt` for a natural male voice, etc.

## Offline Testing (no FreeSWITCH)

Process a WAV file through PersonaPlex directly:

```bash
docker run --rm -it \
  -e HF_TOKEN=hf_your_token \
  -e MOSHI_DEVICE=cpu \
  -v $(pwd)/cache:/app/cache \
  -v $(pwd)/test:/data \
  personaplex-agent:cpu \
  python /app/app.py --offline \
    --input-wav /data/caller.wav \
    --output-wav /data/agent_response.wav \
    --output-text /data/transcript.json
```