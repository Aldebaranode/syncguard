package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"io"

	"golang.org/x/crypto/hkdf"
)

const (
	SALT_SIZE  = 16
	NONCE_SIZE = 12
	KEY_SIZE   = 32
)

func Encrypt(data []byte, secret string) ([]byte, error) {
	hash := sha256.New
	salt := make([]byte, SALT_SIZE)

	if _, err := io.ReadFull(rand.Reader, salt); err != nil {
		return nil, err
	}

	key := make([]byte, KEY_SIZE)
	hkdfStream := hkdf.New(hash, []byte(secret), salt, nil)

	if _, err := io.ReadFull(hkdfStream, key); err != nil {
		return nil, err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}

	aesgcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}

	nonce := make([]byte, NONCE_SIZE)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}

	ciphertext := aesgcm.Seal(nil, nonce, data, nil)

	resultLength := SALT_SIZE + NONCE_SIZE + len(ciphertext)
	result := make([]byte, resultLength)

	copy(result, salt)
	copy(result[SALT_SIZE:], nonce)
	copy(result[SALT_SIZE+NONCE_SIZE:], ciphertext)

	return result, nil
}

func Decrypt(data []byte, secret string) ([]byte, error) {
	salt := data[:SALT_SIZE]
	nonce := data[SALT_SIZE : SALT_SIZE+NONCE_SIZE]
	ciphertext := data[SALT_SIZE+NONCE_SIZE:]

	hash := sha256.New
	hkdfStream := hkdf.New(hash, []byte(secret), salt, nil)

	key := make([]byte, KEY_SIZE)
	if _, err := io.ReadFull(hkdfStream, key); err != nil {
		return nil, err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}

	aesgcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}

	plaintext, err := aesgcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, err
	}

	return plaintext, nil
}
