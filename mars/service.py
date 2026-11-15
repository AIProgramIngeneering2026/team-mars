from .inputs import parse_request


class JobService:
    def __init__(self, jobs, registry):
        self.jobs = jobs
        self.registry = registry

    def create(self, client, key, payload):
        video_id, operation = parse_request(payload, key, self.registry.operations())
        return self.jobs.create(client, key, video_id, operation)

    def get(self, client, job_id):
        return self.jobs.get(client, job_id).public()

    def cancel(self, client, job_id):
        return self.jobs.cancel(client, job_id)
