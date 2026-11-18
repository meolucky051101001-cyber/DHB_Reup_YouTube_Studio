"""Give lifecycle-only fixtures an explicitly confirmed metadata policy."""
import monitor_policy


def confirm_monitor(conn, monitor_id):
    channel_id = conn.execute("INSERT INTO youtube_channels(name,channel_id,default_privacy,studio_category_mode) VALUES('Lifecycle fixture',?,'private','keep')",
                              ('UC' + str(monitor_id).zfill(22),)).lastrowid
    mon = {'target_channel_id':channel_id, 'metadata_policy_json':monitor_policy.encode(monitor_policy.DEFAULT_POLICY)}
    revision = monitor_policy.preview(conn, mon)['revision']
    conn.execute('UPDATE monitors SET target_channel_id=?,metadata_policy_json=?,metadata_revision=? WHERE id=?',
                 (channel_id, mon['metadata_policy_json'], revision, monitor_id))
