with open('app/sources/dramawave.py', 'r') as f:
    content = f.read()

replacement = """            return EpisodePlayback(
                episode_id=str(episode.episode_number),
                playback_type=pb.get('type') or 'hls',
                playback_url=pb['url'],
                headers=pb.get('headers') or {},
                quality=pb.get('quality') or '',
                master_url=pb.get('master_url'),
                audio_url=pb.get('audio_url'),
                audio_language=pb.get('audio_language'),
                metadata=pb,
            )"""

content = content.replace("""            return EpisodePlayback(
                episode_id=str(episode.episode_number),
                playback_type=pb.get('type') or 'hls',
                playback_url=pb['url'],
                headers=pb.get('headers') or {},
                quality=pb.get('quality') or '',
                master_url=pb.get('master_url'),
                audio_url=pb.get('audio_url'),
                audio_language=pb.get('audio_language'),
            )""", replacement)

with open('app/sources/dramawave.py', 'w') as f:
    f.write(content)
