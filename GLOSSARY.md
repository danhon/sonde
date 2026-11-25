# sonde

sonde watches who follows one Bluesky account, who arrives and leaves, and which of them matter, for the one person who owns that account.

## Language

### People

**Follower**:
Someone following the operator now.

**Former follower**:
Someone who followed the operator and has since departed.
_Avoid_: lost follower

**Mutual**:
A follower the operator also follows, however that follow came about.

**Follow-back**:
The operator following a follower from inside sonde. The only thing sonde writes to Bluesky.

**Operator**:
The one Bluesky account sonde watches and acts for, and the person who owns it.
_Avoid_: me, owner, user

**Hidden**:
A follower the operator has taken out of view, carrying a hide reason.
_Avoid_: ignored

**Hide reason**:
Why a follower is hidden: *manual* (the operator's own choice) or *moderation list* (a list the operator subscribes to names them). A manual decision, to hide or to restore, is final until the operator changes it; no moderation list overrides it.

**Verified**:
An account Bluesky, or a trusted verifier, has vouched for. Bluesky's term, used as Bluesky defines it.

**Trusted verifier**:
An account Bluesky allows to verify others. Bluesky's term.

**Reader**:
Another program holding an API token. It sees a redacted view and never the operator's pages.
_Avoid_: client, user

### Movement

**Arrival**:
Someone starting to follow the operator. A former follower who returns is a new arrival each time.
_Avoid_: new follower, gain

**Departure**:
A follower who has stopped following the operator. Only a complete full sweep can establish one.
_Avoid_: lost, churn, unfollow

**Unfollow**:
The operator ceasing to follow someone.
_Avoid_: departure

**Full sweep**:
A read of the operator's entire follower list, end to end. The only evidence of a departure.

**Head sweep**:
A read of only the newest end of the follower list, to catch arrivals quickly. It can never show a departure.

### Grouping

**Circle**:
A named set of people the operator has put together privately.
_Avoid_: group, list

**Member**:
A person in a circle. The operator adds members; a person can be a member of several circles.
_Avoid_: tag, tagged

**Archived circle**:
A circle the operator has set aside. It keeps its members and can be restored.

**Merged circle**:
An archived circle whose members were moved into another circle. It cannot be restored; undoing a merge means making a new circle.

**Candidate**:
A circle sonde suggests that the operator has not yet accepted or rejected.
_Avoid_: proposal, suggestion

**List**:
A Bluesky list, which exists on Bluesky itself and is not a circle.

**Institution**:
An employer, university, publication or other body that people belong to.
_Avoid_: organisation, org

**Affiliation**:
One follower's link to one institution.

### Ranking

**People you both know**:
The accounts the operator follows that also follow a given person.
_Avoid_: known followers, shared followers

**Affinity**:
The number of people you both know, for a given follower: whether they sit inside the operator's world. Says nothing about whether the two of them talk.
_Avoid_: closeness, relevance

**Influence**:
How far a follower's own reach extends, beyond their follower count: whether they matter in general. A large account is not necessarily influential.
_Avoid_: popularity, fame

**Relationship**:
How much the operator and a person actually talk, from the interactions between them. One of three separate scores, beside influence and affinity; none is a kind of another.
_Avoid_: closeness, engagement

**Interaction**:
A reply, quote, repost, mention or like between the operator and someone, in either direction. Following is not an interaction.

**Attention scarcity**:
The part of a relationship earned with nothing said: someone who follows very few accounts, the operator among them. Capped so it can never outrank an actual conversation.

### Telling the operator

**Large account**:
An account with more than 10,000 followers. A matter of size alone, not of influence.
_Avoid_: influential, big account

**Notable**:
An account that is verified, a trusted verifier, or a large account, and not hidden. Notability belongs to the account; an arrival or a like is news because of who did it.

**Notable arrival**:
A notable account starting to follow the operator.

**Notable like or repost**:
A notable account liking or reposting one of the operator's posts. Planned (sonde#10), not yet built.

**Alert**:
An email telling the operator, soon after it happens, about a notable arrival or (once sonde#10 is built) every notable like or repost, several to an email. Nothing from before alerting was switched on is ever alerted.

**Digest**:
The daily email of what happened the day before, including any alert that could not be sent.
