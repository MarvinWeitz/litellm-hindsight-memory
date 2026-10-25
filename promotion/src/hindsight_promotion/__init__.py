"""Promote reviewed, anonymized SAP knowledge from customer banks into the global bank.

Kept apart from the proxy plugin (litellm_hindsight): the plugin only ever reads
the global bank, and this job is its only writer.
"""
