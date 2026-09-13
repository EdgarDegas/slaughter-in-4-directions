# Home TV

Watch live TV in Safari and send it to Apple TV with AirPlay.

## Start

With Docker and Docker Compose 2.24 or newer installed, run from the project folder:

```sh
docker compose up -d --build
```

Open **`http://YOUR_SERVER_LAN_IP:8080/`** in Safari, replacing `YOUR_SERVER_LAN_IP` with your server's address, such as `192.168.1.50`.

1. Wait for **Ready to play**, then tap **Watch live**.
2. Tap **AirPlay** or use the native video player's AirPlay control.
3. Choose your Apple TV.

Keep the server, iPhone, and Apple TV on the same home network. The first connection may take a few minutes.

You can also open **`http://YOUR_SERVER_LAN_IP:8080/live.m3u8`** in a player such as VLC. Picture quality depends on the available broadcast.

## Manage

```sh
# Check status
docker compose ps

# View recent logs
docker compose logs --tail=100

# Stop
docker compose down
```
